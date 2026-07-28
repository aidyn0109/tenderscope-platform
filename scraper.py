"""
scraper.py — Логика парсинга реестра договоров goszakup.gov.kz через GraphQL API v2.

Фильтры (соответствуют URL реестра):
  - supplier_biin — БИН поставщика
  - ref_contract_status_id: [190, 460, 450] (Действует / Передан.Действует / Доп.соглашение)
  - ref_subject_type_id: 2 (Работа) — фильтруется в Python
  - crdate: 2026 год — фильтруется в Python

Для каждого договора извлекаются (из раздела «Предметы договора»):
  - Сумма 1 = сумма total_sum_wnds по всем contract_units (без НДС)
  - Сумма 2 = сумма fact_sum_wnds по всем contract_units (без НДС)
  - Общая итоговая сумма = Сумма 1 − Сумма 2

Наименование поставщика — через отдельный запрос к subjects API.
"""

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
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

# Фильтр по году создания и типу предмета
TARGET_YEAR = 2026
TARGET_SUBJECT_TYPE_ID = 2  # Работа

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Типы данных
# ---------------------------------------------------------------------------

@dataclass
class ContractRecord:
    """Одна запись о договоре."""
    bin: str                    # БИН поставщика
    supplier_name: str          # Наименование компании
    contract_number: str        # Номер договора
    description: str            # Краткое содержание
    cr_datetime: str            # Дата создания (YYYY-MM-DD HH:MM:SS)
    amount_planned: float       # Сумма 1 (total_sum_wnds по units)
    amount_actual: float        # Сумма 2 (fact_sum_wnds по units)
    amount_total: float         # Общая итоговая сумма (Сумма1 − Сумма2)
    max_income: float           # Максимальный доход (из формы ввода)
    url: str                    # Ссылка на договор
    error: str = ""             # Ошибка парсинга (если есть)


@dataclass
class ScrapeResult:
    """Результат парсинга для одного БИН."""
    bin: str
    max_income: float = 0.0
    records: list[ContractRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


ProgressCallback = Callable[[int, int, str], None]
RecordCallback = Callable[["ContractRecord"], None]


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
# GraphQL запросы
# ---------------------------------------------------------------------------

_CONTRACT_QUERY = """
query($f: ContractFiltersInput, $after: Int) {
  contract(limit: 50, after: $after, filters: $f) {
    id
    contract_number_sys
    crdate
    description_ru
    supplier_biin
    ref_subject_type_id
    ref_contract_status_id
    fin_year
    contract_units {
      id
      item_price
      fact_sum
    }
  }
}
"""

_SUBJECT_QUERY = """
query($f: SubjectFiltersInput) {
  subjects(filters: $f) {
    bin
    name_ru
  }
}
"""


def _graphql_request(token: str, query: str, variables: dict) -> dict:
    """Выполняет POST к GraphQL endpoint с retry. Возвращает распарсенный JSON."""
    payload = {"query": query, "variables": variables}
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
            logger.warning("GraphQL запрос попытка %d/%d: %s",
                           attempt + 1, RETRY_COUNT + 1, exc)
            if attempt < RETRY_COUNT:
                time.sleep(RETRY_DELAY)
    raise RuntimeError(f"GraphQL запрос завершился ошибкой: {last_exc}")


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

def _to_float(value) -> float:
    """Безопасное преобразование в float."""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _sum_units(units: list[dict], field: str) -> float:
    """Суммирует значения поля field по всем contract_units."""
    return sum(_to_float(u.get(field)) for u in units if u.get(field) is not None)


# ---------------------------------------------------------------------------
# Получение наименования поставщика через Subjects API
# ---------------------------------------------------------------------------

# Глобальный кэш имён поставщиков (БИН → наименование)
_supplier_name_cache: dict[str, str] = {}


def _fetch_supplier_name(token: str, bin_number: str) -> str:
    """Получает наименование поставщика через subjects API. Результаты кэшируются."""
    if bin_number in _supplier_name_cache:
        return _supplier_name_cache[bin_number]

    try:
        resp = _graphql_request(
            token, _SUBJECT_QUERY,
            {"f": {"bin": bin_number}},
        )
        subjects = (resp.get("data") or {}).get("subjects") or []
        if subjects:
            name = (subjects[0].get("name_ru") or "").strip()
            _supplier_name_cache[bin_number] = name
            logger.info("Наименование поставщика %s: %s", bin_number, name)
            return name
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ошибка subjects API для БИН %s: %s", bin_number, exc)

    _supplier_name_cache[bin_number] = ""
    return ""


# ---------------------------------------------------------------------------
# Сбор всех договоров для одного БИН (с пагинацией)
# ---------------------------------------------------------------------------

def _fetch_contracts_for_bin(
    token: str,
    bin_number: str,
    errors_sink: list[str],
) -> list[dict]:
    """Загружает все договоры для БИН через GraphQL API v2 с пагинацией."""
    items: list[dict] = []
    after: int | None = None
    filter_input = {
        "supplier_biin": bin_number,
        "ref_contract_status_id": TARGET_STATUS_IDS,
    }

    while True:
        variables: dict = {"f": filter_input}
        if after is not None:
            variables["after"] = after

        try:
            response = _graphql_request(token, _CONTRACT_QUERY, variables)
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

    # Фильтрация в Python: только тип "Работа" и только 2026 год
    filtered = []
    for item in items:
        # Проверка типа предмета (Работа = 2)
        if item.get("ref_subject_type_id") != TARGET_SUBJECT_TYPE_ID:
            continue
        # Проверка года создания
        crdate = str(item.get("crdate") or "")
        if not crdate.startswith(str(TARGET_YEAR)):
            continue
        filtered.append(item)

    logger.info(
        "БИН %s: загружено %d записей, после фильтрации (subject_type=2, year=%d) — %d",
        bin_number, len(items), TARGET_YEAR, len(filtered),
    )
    return filtered


# ---------------------------------------------------------------------------
# scrape_bin / scrape_all
# ---------------------------------------------------------------------------

def scrape_bin(
    bin_number: str,
    max_income: float,
    token: str,
    on_contract_progress: ProgressCallback | None = None,
    on_record: RecordCallback | None = None,
) -> ScrapeResult:
    """Парсит все договоры для одного БИН."""
    result = ScrapeResult(bin=bin_number, max_income=max_income)

    # Получаем наименование поставщика через subjects API (один раз для БИН)
    supplier_name = _fetch_supplier_name(token, bin_number)

    items = _fetch_contracts_for_bin(token, bin_number, result.errors)

    if not items:
        result.records.append(ContractRecord(
            bin=bin_number,
            supplier_name=supplier_name or "",
            contract_number="",
            description="Договоры не найдены (после фильтрации)",
            cr_datetime="",
            amount_planned=0.0,
            amount_actual=0.0,
            amount_total=0.0,
            max_income=max_income,
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

        cid = item.get("id")
        crdate = (item.get("crdate") or "").strip()
        description = (item.get("description_ru") or "").strip() or "(описание отсутствует)"
        contract_number = (item.get("contract_number_sys") or "").strip()

        # Суммы по предметам договора (contract_units) — без НДС
        units = item.get("contract_units") or []
        amount_planned = _sum_units(units, "item_price")    # Сумма 1: без НДС
        amount_actual = _sum_units(units, "fact_sum")       # Сумма 2: без НДС
        amount_total = round(amount_planned - amount_actual, 2)

        url = CONTRACT_URL_TEMPLATE.format(id=cid) if cid is not None else ""

        record = ContractRecord(
            bin=bin_number,
            supplier_name=supplier_name or "",
            contract_number=contract_number,
            description=description,
            cr_datetime=crdate,
            amount_planned=amount_planned,
            amount_actual=amount_actual,
            amount_total=amount_total,
            max_income=max_income,
            url=url,
            error="",
        )

        result.records.append(record)

        if on_record:
            try:
                on_record(record)
            except Exception as exc:  # noqa: BLE001
                logger.warning("on_record callback error: %s", exc)

    return result


def scrape_all(
    bin_data: list[dict],
    on_bin_start: Callable[[int, int, str], None] | None = None,
    on_contract_progress: ProgressCallback | None = None,
    on_record: RecordCallback | None = None,
) -> list[ScrapeResult]:
    """
    Главная точка входа. Принимает список словарей вида:
        {"bin": "031240001439", "max_income": 500000000.0}
    """
    # Очищаем кэш имён при новом запуске
    _supplier_name_cache.clear()

    token = _get_token()
    results: list[ScrapeResult] = []
    total_bins = len(bin_data)

    for i, entry in enumerate(bin_data, start=1):
        bin_number = entry["bin"]
        max_income = float(entry.get("max_income", 0))

        if on_bin_start:
            try:
                on_bin_start(i, total_bins, bin_number)
            except Exception as exc:  # noqa: BLE001
                logger.warning("on_bin_start callback error: %s", exc)

        logger.info("=== Обработка БИН %s (%d/%d) ===", bin_number, i, total_bins)
        result = scrape_bin(
            bin_number, max_income, token,
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

    test_bins = sys.argv[1:] if len(sys.argv) > 1 else ["031240001439"]

    def _progress(current: int, total: int, msg: str) -> None:
        print(f"  [{current}/{total}] {msg}")

    results = scrape_all(
        [{"bin": b, "max_income": 1_000_000_000.0} for b in test_bins],
        on_bin_start=lambda i, t, b: print(f"\nОбработка БИН {b} ({i}/{t})"),
        on_contract_progress=_progress,
    )
    for res in results:
        print(f"\n=== БИН {res.bin}: {len(res.records)} договоров ===")
        for rec in res.records[:5]:
            print(
                f"  Компания: {rec.supplier_name or '—'} | "
                f"№{rec.contract_number} | {rec.description[:50]:50s} "
                f"| план: {rec.amount_planned:,.0f} | факт: {rec.amount_actual:,.0f} "
                f"| итого: {rec.amount_total:,.2f} ₸ | макс.доход: {rec.max_income:,.0f}"
            )
        if res.errors:
            print(f"  Ошибок: {len(res.errors)}")