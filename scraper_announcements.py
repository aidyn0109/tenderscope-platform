"""
scraper_announcements.py — Парсинг закупочных объявлений через GraphQL API v3.

Все данные тянутся прямыми HTTPS-запросами к
https://ows.goszakup.gov.kz/v3/graphql.
Авторизация — Bearer-токен из env GOSZAKUP_TOKEN или st.secrets["goszakup_token"]
(читается так же как в scraper.py — через _get_token()).
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

GRAPHQL_ENDPOINT = "https://ows.goszakup.gov.kz/v3/graphql"
REQUEST_TIMEOUT = 60
RETRY_COUNT = 2
RETRY_DELAY = 3
PAGE_LIMIT = 50
MAX_RECORDS = 10_000

ANNOUNCEMENT_URL_TEMPLATE = "https://goszakup.gov.kz/ru/announce/index/{id}"

# Фильтры объявлений
TARGET_STATUS_IDS = [210, 220]      # 210 = Завершено, 220 = Формирование протокола итогов
TARGET_SUBJECT_ID = 2               # 2 = Работа
TOTAL_SUM_RANGE = [1_500_000_000, 999_999_999_999]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Типы данных
# ---------------------------------------------------------------------------

@dataclass
class AnnouncementRecord:
    number: int                  # порядковый номер
    name: str                    # наименование объявления
    method: str                  # способ закупки
    start_date: str              # начало приема заявок
    end_date: str                # окончание приема заявок
    sum_amount: float            # сумма закупки
    status: str                  # статус
    winner_name: str             # наименование победителя
    winner_bin: str              # БИН победителя
    winner_price: float          # цена победителя (0 если нет данных)
    url: str                     # гиперссылка на объявление
    has_contracts: bool = False  # есть ли данные во вкладке «Договоры»
    error: str = ""              # ошибка при парсинге


@dataclass
class ScrapeAnnouncementsResult:
    selected_date: str
    records: list[AnnouncementRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


ProgressCallback = Callable[[int, int, str], None]
RecordCallback = Callable[["AnnouncementRecord"], None]


# ---------------------------------------------------------------------------
# Токен авторизации (читается так же как в scraper.py)
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
# GraphQL-запросы
# ---------------------------------------------------------------------------

_TRD_BUY_QUERY = """
query($filter: TrdBuyFiltersInput, $after: Int) {
  TrdBuy(limit: 50, filter: $filter, after: $after) {
    id
    numberAnno
    nameRu
    totalSum
    refBuyStatusId
    startDate
    endDate
    refSubjectTypeId
    refTradeMethodsId
  }
}
"""

_CONTRACT_QUERY = """
query($filter: ContractFiltersInput) {
  Contract(limit: 5, filter: $filter) {
    id
    supplierBiin
    contractSumWnds
    faktSumWnds
    refContractStatusId
    descriptionRu
  }
}
"""

_SUBJECTS_QUERY = """
query($filter: TrdBuyFiltersInput) {
  Subjects(limit: 1, filter: $filter) {
    bin
    nameRu
  }
}
"""


def _graphql_request(token: str, query: str, variables: dict) -> dict:
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
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _status_name(status_id) -> str:
    sid = _to_int(status_id)
    if sid is None:
        return str(status_id) if status_id is not None else ""
    return {
        210: "Завершено",
        220: "Формирование протокола итогов",
    }.get(sid, str(sid))


def _method_name(method_id) -> str:
    mid = _to_int(method_id)
    if mid is None:
        return str(method_id) if method_id is not None else ""
    return {
        1: "Конкурс",
        2: "Аукцион",
        3: "Запрос ценовых предложений",
        4: "Из одного источника",
        5: "Запрос предложений",
        6: "Закупка из одного источника",
    }.get(mid, f"Способ {mid}")


# ---------------------------------------------------------------------------
# Шаг 1 — список объявлений (с пагинацией; дата фильтруется в Python)
# ---------------------------------------------------------------------------

def _fetch_announcements(
    token: str,
    selected_date: str,
    errors_sink: list[str],
) -> list[dict]:
    """Тянет все объявления по фильтру (status+subject+totalSum), затем фильтрует по дате."""
    items: list[dict] = []
    after: int | None = None
    filter_input = {
        "refBuyStatusId": TARGET_STATUS_IDS,
        "refSubjectTypeId": TARGET_SUBJECT_ID,
        "totalSum": TOTAL_SUM_RANGE,
    }

    while True:
        variables: dict = {"filter": filter_input}
        if after is not None:
            variables["after"] = after

        try:
            response = _graphql_request(token, _TRD_BUY_QUERY, variables)
        except Exception as exc:  # noqa: BLE001
            errors_sink.append(f"TrdBuy: {exc}")
            break

        gql_errors = response.get("errors")
        if gql_errors:
            msg = "; ".join(str(e.get("message", e)) for e in gql_errors)
            errors_sink.append(f"GraphQL error: {msg}")
            break

        batch = (response.get("data") or {}).get("TrdBuy") or []
        items.extend(batch)

        page_info = (response.get("extensions") or {}).get("pageInfo") or {}
        has_next = bool(page_info.get("hasNextPage"))
        last_id = page_info.get("lastId")

        if not batch or not has_next or last_id is None or len(items) >= MAX_RECORDS:
            break
        after = last_id

    # Фильтрация по дате — в Python после получения данных.
    # Берём объявления, у которых endDate >= selected_date (по дате,
    # без учёта времени). selected_date — "YYYY-MM-DD", endDate — "YYYY-MM-DD HH:MM:SS".
    filtered = [
        r for r in items
        if r.get("endDate") and str(r["endDate"])[:10] >= selected_date
    ]
    logger.info("TrdBuy: получено %d записей, после фильтра endDate >= %s — %d",
                len(items), selected_date, len(filtered))
    return filtered


# ---------------------------------------------------------------------------
# Шаг 2 — победитель и наличие договоров
# ---------------------------------------------------------------------------

def _fetch_contracts_for_anno(token: str, number_anno: str) -> list[dict]:
    if not number_anno:
        return []
    try:
        resp = _graphql_request(
            token, _CONTRACT_QUERY,
            {"filter": {"trdBuyNumberAnno": number_anno}},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Contract(trdBuyNumberAnno=%s) ошибка: %s", number_anno, exc)
        return []
    return (resp.get("data") or {}).get("Contract") or []


# ---------------------------------------------------------------------------
# Шаг 3 — наименование победителя
# ---------------------------------------------------------------------------

def _fetch_subject_name(token: str, winner_bin: str) -> str:
    if not winner_bin:
        return ""
    try:
        resp = _graphql_request(
            token, _SUBJECTS_QUERY,
            {"filter": {"bin": winner_bin}},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Subjects(bin=%s) ошибка: %s", winner_bin, exc)
        return ""

    subjects = (resp.get("data") or {}).get("Subjects") or []
    if not subjects:
        return ""
    name = (subjects[0].get("nameRu") or "").strip()
    return name


# ---------------------------------------------------------------------------
# Основная функция парсинга
# ---------------------------------------------------------------------------

def scrape_announcements(
    selected_date: str,
    on_progress: ProgressCallback | None = None,
    on_record: RecordCallback | None = None,
) -> ScrapeAnnouncementsResult:
    """Главная функция парсинга объявлений. selected_date — строка "YYYY-MM-DD"."""
    result = ScrapeAnnouncementsResult(selected_date=selected_date)

    try:
        token = _get_token()
    except Exception as exc:  # noqa: BLE001
        result.errors.append(str(exc))
        if on_progress:
            try:
                on_progress(0, 0, f"Ошибка: {exc}")
            except Exception:  # noqa: BLE001
                pass
        return result

    if on_progress:
        try:
            on_progress(0, 0, "Запрос списка объявлений...")
        except Exception:  # noqa: BLE001
            pass

    items = _fetch_announcements(token, selected_date, result.errors)

    if not items:
        logger.warning("Объявления не найдены для даты: %s", selected_date)
        if on_progress:
            try:
                on_progress(0, 0, "Объявления не найдены")
            except Exception:  # noqa: BLE001
                pass
        return result

    total = len(items)
    for idx, item in enumerate(items, start=1):
        if on_progress:
            try:
                on_progress(idx, total, f"Объявление {idx} из {total}")
            except Exception:  # noqa: BLE001
                pass

        ann_id = _to_int(item.get("id"))
        number_anno = (item.get("numberAnno") or "").strip()
        url = ANNOUNCEMENT_URL_TEMPLATE.format(id=ann_id) if ann_id is not None else ""

        record = AnnouncementRecord(
            number=idx,
            name=(item.get("nameRu") or "").strip(),
            method=_method_name(item.get("refTradeMethodsId")),
            start_date=(item.get("startDate") or "").strip(),
            end_date=(item.get("endDate") or "").strip(),
            sum_amount=_to_float(item.get("totalSum")),
            status=_status_name(item.get("refBuyStatusId")),
            winner_name="",
            winner_bin="",
            winner_price=0.0,
            url=url,
            has_contracts=False,
            error="",
        )

        try:
            contracts = _fetch_contracts_for_anno(token, number_anno)
            has_contracts = len(contracts) > 0
            record.has_contracts = has_contracts

            if has_contracts:
                # Победитель: первый договор у которого supplierBiin не пустой
                for c in contracts:
                    biin = (c.get("supplierBiin") or "").strip()
                    if biin:
                        record.winner_bin = biin
                        break
                # has_contracts=True → winner_price = 0.0 (Excel-слой скроет колонку)
                record.winner_price = 0.0
            else:
                # has_contracts=False → договоров нет, источника контрактной суммы нет
                record.winner_price = 0.0

            # Шаг 3: имя победителя через Subjects
            if record.winner_bin:
                record.winner_name = _fetch_subject_name(token, record.winner_bin)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Ошибка обработки объявления id=%s: %s", ann_id, exc)
            record.error = f"Ошибка: {exc}"

        if (not record.winner_bin and not record.winner_name
                and not record.has_contracts and not record.error):
            record.error = "Победитель не найден"

        result.records.append(record)

        if on_record:
            try:
                on_record(record)
            except Exception as exc:  # noqa: BLE001
                logger.warning("on_record callback error: %s", exc)

        if record.error:
            result.errors.append(f"{record.url}: {record.error}")

    return result


# ---------------------------------------------------------------------------
# Быстрый ручной тест
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

    test_date = sys.argv[1] if len(sys.argv) > 1 else "2024-01-15"

    def _progress(current: int, total: int, msg: str) -> None:
        print(f"  [{current}/{total}] {msg}")

    result = scrape_announcements(test_date, on_progress=_progress)
    print(f"\n=== Объявления за {test_date}: {len(result.records)} найдено ===")
    for rec in result.records[:5]:
        print(
            f"  №{rec.number} | {rec.name[:50]:50s} "
            f"| Победитель: {rec.winner_name[:30]:30s} "
            f"| БИН: {rec.winner_bin} | Цена: {rec.winner_price:,.0f} ₸"
        )
    if result.errors:
        print(f"  Ошибок: {len(result.errors)}")
