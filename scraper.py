"""
scraper.py — Логика парсинга реестра договоров goszakup.gov.kz через GraphQL API v2.

Прямые HTTPS-запросы к https://ows.goszakup.gov.kz/v2/graphql.
Авторизация — Bearer-токен из env GOSZAKUP_TOKEN или st.secrets["goszakup_token"].
"""

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:  # streamlit может быть недоступен в subprocess-окружении
    import streamlit as st  # type: ignore
except Exception:  # noqa: BLE001
    st = None  # type: ignore

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

GRAPHQL_ENDPOINT = "https://ows.goszakup.gov.kz/v2/graphql"
REQUEST_TIMEOUT = 60
RETRY_COUNT = 2
RETRY_DELAY = 3
PAGE_LIMIT = 50
MAX_RECORDS = 10_000

CONTRACT_URL_TEMPLATE = "https://goszakup.gov.kz/ru/egzcontract/cpublic/show/{id}"

# 190 = Действует, 460 = Передан.Действует, 450 = Создано доп.соглашение
TARGET_STATUS_IDS = [190, 460, 450]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Типы данных
# ---------------------------------------------------------------------------

@dataclass
class ContractRecord:
    bin:             str
    contract_number: str    # Номер основного договора в реестре договоров
    description:     str    # Краткое содержание договора на русском языке
    validity_period: str    # Срок действия договора
    amount_final:    float  # Общая итоговая сумма договора
    amount_actual:   float  # Общая фактическая сумма договора
    difference:      float  # amount_final - amount_actual
    url:             str
    error:           str = ""


@dataclass
class ScrapeResult:
    bin:     str
    records: list[ContractRecord] = field(default_factory=list)
    errors:  list[str]            = field(default_factory=list)


ProgressCallback = Callable[[int, int, str], None]
RecordCallback   = Callable[["ContractRecord"], None]


# ---------------------------------------------------------------------------
# Токен авторизации
# ---------------------------------------------------------------------------

def _get_token() -> str:
    """Читает Bearer-токен из env или st.secrets. Иначе RuntimeError."""
    token = (os.environ.get("GOSZAKUP_TOKEN") or "").strip()
    if token:
        return token
    if st is not None:
        try:
            secret = st.secrets.get("goszakup_token")  # type: ignore[attr-defined]
            if secret:
                secret = str(secret).strip()
                if secret:
                    return secret
        except Exception:  # noqa: BLE001
            pass
    raise RuntimeError(
        "Токен Goszakup API не найден. Установите переменную окружения "
        "GOSZAKUP_TOKEN или ключ st.secrets['goszakup_token']."
    )


# ---------------------------------------------------------------------------
# GraphQL запрос
# ---------------------------------------------------------------------------

_CONTRACT_QUERY = """
query($filter: ContractFiltersInput, $after: Int) {
  contract(limit: 50, after: $after, filters: $filter) {
    id
    contract_number_sys
    description_ru
    contract_sum_wnds
    fakt_sum_wnds
    ref_contract_status_id
    supplier_biin
    sign_date
    ec_end_date
  }
}
"""


def _graphql_request(token: str, variables: dict) -> dict:
    """Выполняет POST к GraphQL endpoint с retry. Возвращает распарсенный JSON."""
    payload = {"query": _CONTRACT_QUERY, "variables": variables}
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    last_exc: Exception | None = None
    for attempt in range(RETRY_COUNT + 1):
        try:
            resp = requests.post(
                GRAPHQL_ENDPOINT,
                json=payload,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
                verify=False,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning("GraphQL contract запрос попытка %d/%d: %s",
                           attempt + 1, RETRY_COUNT + 1, exc)
            if attempt < RETRY_COUNT:
                time.sleep(RETRY_DELAY)
    raise RuntimeError(f"GraphQL запрос завершился ошибкой: {last_exc}")


# ---------------------------------------------------------------------------
# Форматирование
# ---------------------------------------------------------------------------

def _format_date(s: str | None) -> str:
    """Парсит дату YYYY-MM-DD[ HH:MM:SS] → DD.MM.YYYY. Пустая строка/исключение → ''. """
    if not s:
        return ""
    head = str(s).strip()[:10]
    try:
        y, m, d = head.split("-")
        return f"{int(d):02d}.{int(m):02d}.{int(y):04d}"
    except Exception:  # noqa: BLE001
        return str(s).strip()


def _format_validity_period(sign_date: str | None, end_date: str | None) -> str:
    a = _format_date(sign_date)
    b = _format_date(end_date)
    if a and b:
        return f"{a} — {b}"
    return a or b or ""


def _to_float(value) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _contract_record_from_item(item: dict, bin_number: str) -> ContractRecord:
    cid = item.get("id")
    amount_final = _to_float(item.get("contract_sum_wnds"))
    amount_actual = _to_float(item.get("fakt_sum_wnds"))
    description = (item.get("description_ru") or "").strip() or "(описание отсутствует)"
    url = CONTRACT_URL_TEMPLATE.format(id=cid) if cid is not None else ""
    return ContractRecord(
        bin=(item.get("supplier_biin") or bin_number),
        contract_number=(item.get("contract_number_sys") or "").strip(),
        description=description,
        validity_period=_format_validity_period(
            item.get("sign_date"), item.get("ec_end_date")
        ),
        amount_final=amount_final,
        amount_actual=amount_actual,
        difference=round(amount_final - amount_actual, 2),
        url=url,
        error="",
    )


# ---------------------------------------------------------------------------
# Сбор всех договоров для одного БИН (с пагинацией)
# ---------------------------------------------------------------------------

def _fetch_contracts_for_bin(
    token: str,
    bin_number: str,
    errors_sink: list[str],
) -> list[dict]:
    items: list[dict] = []
    after: int | None = None
    filter_input = {
        "supplier_biin": bin_number,
        "ref_contract_status_id": TARGET_STATUS_IDS,
    }

    while True:
        variables: dict = {"filter": filter_input}
        if after is not None:
            variables["after"] = after

        try:
            response = _graphql_request(token, variables)
        except Exception as exc:  # noqa: BLE001
            errors_sink.append(f"БИН {bin_number}: {exc}")
            break

        gql_errors = response.get("errors")
        if gql_errors:
            msg = "; ".join(str(e.get("message", e)) for e in gql_errors)
            errors_sink.append(f"GraphQL error: {msg}")
            break

        data = response.get("data") or {}
        batch = data.get("contract") or []
        items.extend(batch)

        page_info = (response.get("extensions") or {}).get("pageInfo") or {}
        has_next = bool(page_info.get("hasNextPage"))
        last_id = page_info.get("lastId")

        if not batch or not has_next or last_id is None or len(items) >= MAX_RECORDS:
            break
        after = last_id

    return items


# ---------------------------------------------------------------------------
# scrape_bin / scrape_all
# ---------------------------------------------------------------------------

def scrape_bin(
    bin_number: str,
    token: str,
    on_contract_progress: ProgressCallback | None = None,
    on_record: RecordCallback | None = None,
) -> ScrapeResult:
    result = ScrapeResult(bin=bin_number)

    items = _fetch_contracts_for_bin(token, bin_number, result.errors)

    if not items:
        result.records.append(ContractRecord(
            bin=bin_number,
            contract_number="",
            description="Договоры не найдены",
            validity_period="",
            amount_final=0.0,
            amount_actual=0.0,
            difference=0.0,
            url="",
            error="Договоры не найдены",
        ))
        return result

    total = len(items)
    for idx, item in enumerate(items, start=1):
        if on_contract_progress:
            try:
                on_contract_progress(idx, total, f"Договор {idx} из {total}")
            except Exception as exc:  # noqa: BLE001
                logger.warning("on_contract_progress callback error: %s", exc)

        record = _contract_record_from_item(item, bin_number)
        result.records.append(record)

        if on_record:
            try:
                on_record(record)
            except Exception as exc:  # noqa: BLE001
                logger.warning("on_record callback error: %s", exc)

    return result


def scrape_all(
    bin_list: list[str],
    on_bin_start: Callable[[int, int, str], None] | None = None,
    on_contract_progress: ProgressCallback | None = None,
    on_record: RecordCallback | None = None,
) -> list[ScrapeResult]:
    token = _get_token()
    results: list[ScrapeResult] = []
    total_bins = len(bin_list)

    for i, bin_number in enumerate(bin_list, start=1):
        if on_bin_start:
            try:
                on_bin_start(i, total_bins, bin_number)
            except Exception as exc:  # noqa: BLE001
                logger.warning("on_bin_start callback error: %s", exc)

        logger.info("=== Обработка БИН %s (%d/%d) ===", bin_number, i, total_bins)
        result = scrape_bin(
            bin_number, token,
            on_contract_progress=on_contract_progress,
            on_record=on_record,
        )
        results.append(result)

    return results


# ---------------------------------------------------------------------------
# Быстрый ручной тест
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

    test_bins = sys.argv[1:] if len(sys.argv) > 1 else ["051040005224"]

    def _progress(current: int, total: int, msg: str) -> None:
        print(f"  [{current}/{total}] {msg}")

    results = scrape_all(
        test_bins,
        on_bin_start=lambda i, t, b: print(f"\nОбработка БИН {b} ({i}/{t})"),
        on_contract_progress=_progress,
    )
    for res in results:
        print(f"\n=== БИН {res.bin}: {len(res.records)} договоров ===")
        for rec in res.records[:5]:
            print(
                f"  №{rec.contract_number} | {rec.description[:50]:50s} "
                f"| итог: {rec.amount_final:,.0f} | факт: {rec.amount_actual:,.0f} "
                f"| разница: {rec.difference:,.2f} ₸"
            )
        if res.errors:
            print(f"  Ошибок: {len(res.errors)}")
