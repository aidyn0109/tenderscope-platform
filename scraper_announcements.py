"""
scraper_announcements.py — Парсинг закупочных объявлений через GraphQL API v3.

Все данные тянутся прямыми HTTPS-запросами к
https://ows.goszakup.gov.kz/v3/graphql (объявления и лоты) и
https://ows.goszakup.gov.kz/v2/graphql (договоры).
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

GRAPHQL_V3_ENDPOINT = "https://ows.goszakup.gov.kz/v3/graphql"
GRAPHQL_V2_ENDPOINT = "https://ows.goszakup.gov.kz/v2/graphql"
REQUEST_TIMEOUT = 60
RETRY_COUNT = 2
RETRY_DELAY = 3
PAGE_LIMIT = 50
MAX_RECORDS = 10_000

ANNOUNCEMENT_URL_TEMPLATE = "https://goszakup.gov.kz/ru/announce/index/{id}"

# 210 = Завершено, 220 = Формирование протокола итогов
TARGET_STATUS_IDS = [210, 220]
STATUS_MAP = {
    210: "Завершено",
    220: "Формирование протокола итогов",
}

# 2 = Работа
TARGET_SUBJECT_IDS = [2]

MIN_SUM_AMOUNT = 1_500_000_000

METHOD_MAP = {
    1: "Конкурс",
    2: "Аукцион",
    3: "Запрос ценовых предложений",
    4: "Из одного источника",
}

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
# Токен авторизации
# ---------------------------------------------------------------------------

def _get_token() -> str:
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
  trd_buy(limit: 50, after: $after, filters: $filter) {
    id
    name_ru
    number_anno
    ref_buy_status_id
    start_date
    end_date
    price
    ref_type_trade_id
  }
}
"""

_LOTS_BY_TRD_BUY_QUERY = """
query($id: Int) {
  trd_buy(filters: { id: [$id] }) {
    id
    lots {
      id
      winner_id
      winner_bin
      winner_name_ru
    }
  }
}
"""

_CONTRACT_EXISTS_QUERY = """
query($anno: String) {
  contract(limit: 1, filters: { trd_buy_number_anno: $anno }) {
    id
  }
}
"""

_CONTRACT_BY_FILTER_QUERY = """
query($filter: ContractFiltersInput) {
  contract(limit: 1, filters: $filter) {
    id
    contract_sum_wnds
    supplier_biin
  }
}
"""

_LOTS_QUERY = """
query($filter: LotsFiltersInput) {
  lots(limit: 1, filters: $filter) {
    id
    budget
    winner_price
  }
}
"""


def _graphql_request(endpoint: str, token: str, query: str, variables: dict) -> dict:
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
                endpoint, json=payload, headers=headers,
                timeout=REQUEST_TIMEOUT, verify=False,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning("GraphQL %s запрос попытка %d/%d: %s",
                           endpoint, attempt + 1, RETRY_COUNT + 1, exc)
            if attempt < RETRY_COUNT:
                time.sleep(RETRY_DELAY)
    raise RuntimeError(f"GraphQL запрос {endpoint} завершился ошибкой: {last_exc}")


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


def _method_label(ref_id) -> str:
    rid = _to_int(ref_id)
    if rid is None:
        return ""
    return METHOD_MAP.get(rid, str(rid))


def _status_label(ref_id) -> str:
    rid = _to_int(ref_id)
    if rid is None:
        return ""
    return STATUS_MAP.get(rid, str(rid))


def _passthrough_date(s: str | None) -> str:
    return (s or "").strip()


# ---------------------------------------------------------------------------
# Шаг 1 — список объявлений (с пагинацией)
# ---------------------------------------------------------------------------

def _fetch_announcements(
    token: str,
    selected_date: str,
    errors_sink: list[str],
) -> list[dict]:
    items: list[dict] = []
    after: int | None = None
    filter_input = {
        "ref_buy_status_id": TARGET_STATUS_IDS,
        "ref_subject_type_id": TARGET_SUBJECT_IDS,
        "end_date_gte": selected_date,
        "price_gte": MIN_SUM_AMOUNT,
    }

    while True:
        variables: dict = {"filter": filter_input}
        if after is not None:
            variables["after"] = after

        try:
            response = _graphql_request(
                GRAPHQL_V3_ENDPOINT, token, _TRD_BUY_QUERY, variables
            )
        except Exception as exc:  # noqa: BLE001
            errors_sink.append(f"trd_buy: {exc}")
            break

        gql_errors = response.get("errors")
        if gql_errors:
            msg = "; ".join(str(e.get("message", e)) for e in gql_errors)
            errors_sink.append(f"GraphQL error: {msg}")
            break

        batch = (response.get("data") or {}).get("trd_buy") or []
        items.extend(batch)

        page_info = (response.get("extensions") or {}).get("pageInfo") or {}
        has_next = bool(page_info.get("hasNextPage"))
        last_id = page_info.get("lastId")

        if not batch or not has_next or last_id is None or len(items) >= MAX_RECORDS:
            break
        after = last_id

    return items


# ---------------------------------------------------------------------------
# Шаг 2 — победитель
# ---------------------------------------------------------------------------

def _fetch_winner(token: str, ann_id: int) -> tuple[str, str]:
    """Возвращает (winner_bin, winner_name_ru) или ('', '')."""
    try:
        resp = _graphql_request(
            GRAPHQL_V3_ENDPOINT, token, _LOTS_BY_TRD_BUY_QUERY,
            {"id": int(ann_id)},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("_fetch_winner(id=%s) ошибка: %s", ann_id, exc)
        return "", ""

    trd = (resp.get("data") or {}).get("trd_buy") or []
    if not trd:
        return "", ""
    lots = trd[0].get("lots") or []
    for lot in lots:
        bin_value = (lot.get("winner_bin") or "").strip()
        name_value = (lot.get("winner_name_ru") or "").strip()
        if bin_value:
            return bin_value, name_value
    return "", ""


# ---------------------------------------------------------------------------
# Шаг 3 — проверка наличия договоров
# ---------------------------------------------------------------------------

def _check_has_contracts(token: str, number_anno: str) -> bool:
    if not number_anno:
        return False
    try:
        resp = _graphql_request(
            GRAPHQL_V2_ENDPOINT, token, _CONTRACT_EXISTS_QUERY,
            {"anno": number_anno},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("_check_has_contracts(%s) ошибка: %s", number_anno, exc)
        return False
    contracts = (resp.get("data") or {}).get("contract") or []
    return bool(contracts)


# ---------------------------------------------------------------------------
# Шаг 4 — цена победителя
# ---------------------------------------------------------------------------

def _fetch_winner_price(
    token: str,
    number_anno: str,
    winner_bin: str,
    ann_id: int | None,
) -> float:
    # 4а) Договор по объявлению + БИН поставщика
    if number_anno and winner_bin:
        try:
            resp = _graphql_request(
                GRAPHQL_V2_ENDPOINT, token, _CONTRACT_BY_FILTER_QUERY,
                {"filter": {
                    "trd_buy_number_anno": number_anno,
                    "supplier_biin": winner_bin,
                }},
            )
            contracts = (resp.get("data") or {}).get("contract") or []
            if contracts:
                amount = _to_float(contracts[0].get("contract_sum_wnds"))
                if amount > 0:
                    return amount
        except Exception as exc:  # noqa: BLE001
            logger.warning("_fetch_winner_price contract ошибка: %s", exc)

    # 4б) Фолбэк — данные лота
    if ann_id is not None:
        try:
            resp = _graphql_request(
                GRAPHQL_V3_ENDPOINT, token, _LOTS_QUERY,
                {"filter": {"trd_buy_id": [int(ann_id)]}},
            )
            lots = (resp.get("data") or {}).get("lots") or []
            if lots:
                lot = lots[0]
                for key in ("winner_price", "budget"):
                    value = _to_float(lot.get(key))
                    if value > 0:
                        return value
        except Exception as exc:  # noqa: BLE001
            logger.warning("_fetch_winner_price lots ошибка: %s", exc)

    return 0.0


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
        number_anno = (item.get("number_anno") or "").strip()
        url = ANNOUNCEMENT_URL_TEMPLATE.format(id=ann_id) if ann_id is not None else ""

        record = AnnouncementRecord(
            number=idx,
            name=(item.get("name_ru") or "").strip(),
            method=_method_label(item.get("ref_type_trade_id")),
            start_date=_passthrough_date(item.get("start_date")),
            end_date=_passthrough_date(item.get("end_date")),
            sum_amount=_to_float(item.get("price")),
            status=_status_label(item.get("ref_buy_status_id")),
            winner_name="",
            winner_bin="",
            winner_price=0.0,
            url=url,
            has_contracts=False,
            error="",
        )

        try:
            if ann_id is not None:
                winner_bin, winner_name = _fetch_winner(token, ann_id)
            else:
                winner_bin, winner_name = "", ""
            record.winner_bin = winner_bin
            record.winner_name = winner_name

            has_contracts = _check_has_contracts(token, number_anno)
            record.has_contracts = has_contracts

            if not has_contracts:
                record.winner_price = _fetch_winner_price(
                    token, number_anno, winner_bin, ann_id
                )
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
