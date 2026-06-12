"""
scraper_announcements.py — Парсинг закупочных объявлений через GraphQL API v3.

Фильтрация по диапазону дат публикации итогов (itogiDatePublic),
что соответствует полю "Протокол итогов с/по" на сайте госзакупок.
Победитель и цена извлекаются из HTML-протокола итогов.

"""

import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    import streamlit as st  # type: ignore
except Exception:  # noqa: BLE001
    st = None  # type: ignore

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

GRAPHQL_ENDPOINT = "https://ows.goszakup.gov.kz/v3/graphql"
REQUEST_TIMEOUT = 60
PROTOCOL_TIMEOUT = 30
RETRY_COUNT = 2
RETRY_DELAY = 3
MAX_RECORDS = 10_000

ANNOUNCEMENT_URL_TEMPLATE = "https://goszakup.gov.kz/ru/announce/index/{id}"
LOT_PAGE_URL_TEMPLATE = "https://goszakup.gov.kz/ru/lots/index/{lot_id}"

TARGET_STATUS_IDS = [210, 220, 330, 350]
TARGET_SUBJECT_ID = 2
TOTAL_SUM_RANGE = [1_500_000_000, 999_999_999_999]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Типы данных
# ---------------------------------------------------------------------------

@dataclass
class LotRecord:
    """Данные по одному лоту внутри объявления."""
    lot_number: str       # номер лота (например "86606608-ОТ1")
    lot_name: str         # наименование лота
    lot_amount: float     # выделенная сумма лота
    winner_name: str      # наименование победителя
    winner_bin: str       # БИН победителя
    winner_price: float   # цена победителя
    year1_sum: float = 0.0  # сумма 1-го года (из страницы лота)


@dataclass
class AnnouncementRecord:
    number: int
    name: str
    method: str
    start_date: str
    end_date: str
    sum_amount: float
    status: str
    winner_name: str      # победитель первого лота (или единственного)
    winner_bin: str
    winner_price: float
    url: str
    has_contracts: bool = False
    error: str = ""
    lots: list[LotRecord] = field(default_factory=list)  # все лоты


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
  TrdBuy(limit: 50, filter: $filter, after: $after) {
    id
    numberAnno
    nameRu
    totalSum
    countLots
    refBuyStatusId
    startDate
    endDate
    itogiDatePublic
    refSubjectTypeId
    refTradeMethodsId
  }
}
"""

_TRD_BUY_FILES_QUERY = """
query($filter: TrdBuyFiltersInput) {
  TrdBuy(limit: 1, filter: $filter) {
    id
    numberAnno
    Files {
      id
      nameRu
      filePath
    }
  }
}
"""

_CONTRACT_QUERY = """
query($filter: ContractFiltersInput) {
  Contract(limit: 5, filter: $filter) {
    id
    supplierBiin
    contractSumWnds
    refContractStatusId
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

_LOTS_QUERY = """
query($filter: TrdBuyFiltersInput) {
  Lots(limit: 100, filter: $filter) {
    id
    lotNumber
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


def _clean_number(s: str) -> float:
    cleaned = (s.replace("\xa0", "")
                .replace(" ", "")
                .replace("\u202f", "")
                .replace(",", "."))
    return float(cleaned)


def _status_name(status_id) -> str:
    sid = _to_int(status_id)
    if sid is None:
        return str(status_id) if status_id is not None else ""
    return {
        210: "Завершено",
        220: "Формирование протокола итогов",
        330: "Итоги опубликованы",
        350: "Договор подписан",
    }.get(sid, str(sid))


def _method_name(method_id) -> str:
    mid = _to_int(method_id)
    if mid is None:
        return str(method_id) if method_id is not None else ""
    return {
        1:   "Конкурс",
        2:   "Аукцион",
        3:   "Запрос ценовых предложений",
        4:   "Из одного источника",
        5:   "Запрос предложений",
        6:   "Закупка из одного источника",
        188: "Конкурс (строительство)",
        201: "Конкурс с предквалификацией",
    }.get(mid, f"Способ {mid}")


def _is_numbering_row(cells: list[str]) -> bool:
    if not cells:
        return False
    non_empty = [c for c in cells if c.strip()]
    if not non_empty:
        return False
    try:
        nums = [int(c.strip()) for c in non_empty]
        return nums == list(range(1, len(nums) + 1))
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Шаг 1 — список объявлений
# ---------------------------------------------------------------------------

def _fetch_announcements(
    token: str,
    date_from: str,
    date_to: str,
    errors_sink: list[str],
) -> list[dict]:
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

    filtered = [
        r for r in items
        if r.get("itogiDatePublic")
        and date_from <= str(r["itogiDatePublic"])[:10] <= date_to
    ]
    logger.info(
        "TrdBuy: получено %d записей, после фильтра itogiDatePublic [%s, %s] — %d",
        len(items), date_from, date_to, len(filtered),
    )
    return filtered


# ---------------------------------------------------------------------------
# Шаг 2 — договоры
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
        logger.warning("Contract(numberAnno=%s) ошибка: %s", number_anno, exc)
        return []
    return (resp.get("data") or {}).get("Contract") or []


# ---------------------------------------------------------------------------
# Шаг 3 — файлы объявления
# ---------------------------------------------------------------------------

def _fetch_files_for_anno(token: str, ann_id: int) -> list[dict]:
    if not ann_id:
        return []
    try:
        resp = _graphql_request(
            token, _TRD_BUY_FILES_QUERY,
            {"filter": {"id": [ann_id]}},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("TrdBuy Files(id=%s) ошибка: %s", ann_id, exc)
        return []
    items = (resp.get("data") or {}).get("TrdBuy") or []
    if not items:
        return []
    return items[0].get("Files") or []


def _download_protocol(token: str, url: str):
    if not url:
        return None
    try:
        r = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            verify=False,
            timeout=PROTOCOL_TIMEOUT,
        )
        r.raise_for_status()
        return BeautifulSoup(r.content, "html.parser")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ошибка скачивания протокола %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Шаг 4 — парсинг HTML протокола
# ---------------------------------------------------------------------------

def _get_table_header(table) -> str:
    """Возвращает объединённый текст первых 3 строк таблицы (нижний регистр)."""
    rows = table.find_all("tr")
    return " ".join(
        rows[i].get_text(" ", strip=True).lower()
        for i in range(min(3, len(rows)))
    )


def _check_failed_purchase(tables) -> bool:
    """
    Задача 1.2: Проверяет есть ли в протоколе маркер "Закупка не состоялась".
    Возвращает True если закупка не состоялась.
    """
    markers = [
        "закупка не состоялась",
        "мемлекеттік сатып алу өткізілмеді",
        "закупки не состоялись",
    ]
    for table in tables:
        text = table.get_text(" ", strip=True).lower()
        for marker in markers:
            if marker in text:
                logger.info("Обнаружен маркер 'закупка не состоялась'")
                return True
    return False


def _find_single_admitted_supplier(tables) -> tuple[str, str]:
    """
    Задача 1.1: Fallback для протоколов с одним участником.
    Ищет таблицу допущенных участников ("допущен" / "жіберілді") и возвращает
    единственного участника как победителя если он там один.
    Возвращает (name, bin).
    """
    admission_markers = [
        "допущен",
        "жіберілді",
        "к участию допущен",
        "участники, допущенные",
        "к участию в конкурсе",
    ]
    for table in tables:
        header = _get_table_header(table)
        if not any(m in header for m in admission_markers):
            continue
        rows = table.find_all("tr")
        supplier_rows = []
        for row in rows[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            if len(cells) < 3:
                continue
            if _is_numbering_row(cells):
                continue
            name = cells[1].strip() if len(cells) > 1 else ""
            bin_val = cells[2].replace(" ", "").strip() if len(cells) > 2 else ""
            if name and bin_val and bin_val.isdigit() and len(bin_val) == 12:
                supplier_rows.append((name, bin_val))
        if len(supplier_rows) == 1:
            logger.info(
                "Единственный допущенный участник (fallback): %s / %s",
                supplier_rows[0][0], supplier_rows[0][1],
            )
            return supplier_rows[0]
    return "", ""


def _parse_price_table(table) -> list[tuple[str, str, float]]:
    """
    Парсит таблицу цен. Возвращает список (name, bin, price).
    Столбцы: № | Наименование | БИН | Выделенная сумма | Цена поставщика | ...
    """
    results = []
    rows = table.find_all("tr")
    for row in rows[1:]:
        cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
        if len(cells) < 5:
            continue
        if _is_numbering_row(cells):
            continue
        name = cells[1].strip()
        bin_val = cells[2].replace(" ", "").strip()
        if not name or not bin_val:
            continue
        try:
            price = _clean_number(cells[4])
            if price > 0:
                results.append((name, bin_val, price))
        except (ValueError, IndexError):
            pass
    return results


def _parse_lots_table(table) -> list[tuple[str, str, float]]:
    """
    Парсит таблицу списка лотов (русская часть: "№ лота | Наименование лота | ...").
    Возвращает список (lot_number, lot_name, lot_amount).
    """
    lots = []
    rows = table.find_all("tr")
    for row in rows[1:]:
        cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
        if len(cells) < 5:
            continue
        if _is_numbering_row(cells):
            continue
        lot_num = cells[1].strip()
        lot_name = cells[2].strip()
        if not lot_num:
            continue
        try:
            amount = _clean_number(cells[5]) if len(cells) > 5 else _clean_number(cells[4])
        except (ValueError, IndexError):
            amount = 0.0
        lots.append((lot_num, lot_name, amount))
    return lots


def _parse_protocol_lots(tables: list) -> list[LotRecord]:
    """
    Разбирает протокол с N лотами.

    Структура протокола:
    - Таблица "№ лота | Наименование лота" — список всех лотов по порядку
    - После неё блоки по 8 таблиц на каждый лот, в каждом блоке есть
      таблица цен ("цена поставщика" или "өнім берушінің бағасы")

    Алгоритм:
    1. Находим русскую таблицу лотов (заголовок "№ лота | наименование лота")
    2. Собираем лоты по порядку из этой таблицы
    3. Ищем все таблицы цен ПОСЛЕ таблицы лотов (по 1 на каждый лот)
    4. Сопоставляем лоты с таблицами цен по порядку
    """
    lot_table_idx = None
    lots_data: list[tuple[str, str, float]] = []

    for i, table in enumerate(tables):
        header = _get_table_header(table)
        if "лота" in header and "наименование" in header and "количество" in header:
            candidate = _parse_lots_table(table)
            if candidate:
                lot_table_idx = i
                lots_data = candidate
                logger.info("Таблица лотов найдена: таблица %d, лотов: %d",
                            i + 1, len(candidate))
                break

    if not lots_data:
        return []

    price_tables = []
    for i in range(lot_table_idx + 1, len(tables)):
        header = _get_table_header(tables[i])
        if "цена поставщика" in header or "өнім берушінің бағасы" in header:
            price_tables.append(tables[i])

    logger.info("Таблиц цен найдено: %d для %d лотов", len(price_tables), len(lots_data))

    lot_records = []
    for idx, (lot_num, lot_name, lot_amount) in enumerate(lots_data):
        if idx >= len(price_tables):
            lot_records.append(LotRecord(
                lot_number=lot_num,
                lot_name=lot_name,
                lot_amount=lot_amount,
                winner_name="",
                winner_bin="",
                winner_price=0.0,
            ))
            continue

        prices = _parse_price_table(price_tables[idx])
        if not prices:
            lot_records.append(LotRecord(
                lot_number=lot_num,
                lot_name=lot_name,
                lot_amount=lot_amount,
                winner_name="",
                winner_bin="",
                winner_price=0.0,
            ))
            continue

        winner = min(prices, key=lambda x: x[2])
        lot_records.append(LotRecord(
            lot_number=lot_num,
            lot_name=lot_name,
            lot_amount=lot_amount,
            winner_name=winner[0],
            winner_bin=winner[1],
            winner_price=winner[2],
        ))
        logger.info("Лот %s: победитель %s, цена %.2f", lot_num, winner[1], winner[2])

    return lot_records


def _find_winner_from_protocol(tables) -> tuple[str, str]:
    """Для однолотовых — ищет таблицу победителя."""
    for table in tables:
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        header_text = rows[0].get_text().lower()
        if "победител" not in header_text and "жеңімпаз" not in header_text:
            continue
        for row in rows[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            if len(cells) < 3:
                continue
            if _is_numbering_row(cells):
                continue
            name = cells[1].strip()
            bin_val = cells[2].replace(" ", "").strip()
            if name and bin_val.isdigit() and len(bin_val) == 12:
                return name, bin_val
    return "", ""


def _find_winner_price_from_protocol(tables, winner_bin: str) -> float:
    """Для однолотовых — ищет цену победителя."""
    if not winner_bin:
        return 0.0

    # Тип 1: таблица с "цена поставщика"
    for table in tables:
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        header_text = " ".join(
            rows[i].get_text(" ", strip=True).lower()
            for i in range(min(3, len(rows)))
        )
        if ("цена поставщика" not in header_text
                and "өнім берушінің бағасы" not in header_text):
            continue
        for row in rows[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            if _is_numbering_row(cells):
                continue
            if len(cells) < 5:
                continue
            for col_idx, cell in enumerate(cells):
                if cell.replace(" ", "").strip() == winner_bin:
                    price_col = col_idx + 2
                    if price_col < len(cells):
                        try:
                            price = _clean_number(cells[price_col])
                            if price > 0:
                                return price
                        except (ValueError, IndexError):
                            pass

    # Тип 2: рейтингово-балльная
    for table in tables:
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        header_text = " ".join(
            rows[i].get_text(" ", strip=True).lower()
            for i in range(min(3, len(rows)))
        )
        has_balls = ("суммарное количество баллов" in header_text
                     or "баллдардың жиынтық саны" in header_text)
        has_sum = ("выделенная сумма" in header_text
                   or "бөлінген сома" in header_text)
        if not (has_balls and has_sum):
            continue
        for row in rows[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            if _is_numbering_row(cells):
                continue
            for col_idx, cell in enumerate(cells):
                if cell.replace(" ", "").strip() == winner_bin:
                    price_col = col_idx + 1
                    if price_col < len(cells):
                        try:
                            price = _clean_number(cells[price_col])
                            if price > 0:
                                return price
                        except (ValueError, IndexError):
                            pass

    # Тип 3: единственный участник — берём выделенную сумму из таблицы лота
    for table in tables:
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        header_text = " ".join(
            rows[i].get_text(" ", strip=True).lower()
            for i in range(min(3, len(rows)))
        )
        has_sum = ("выделенная сумма" in header_text or "бөлінген сома" in header_text)
        if not has_sum:
            continue
        for row in rows[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            if _is_numbering_row(cells):
                continue
            for col_idx, cell in enumerate(cells):
                if cell.replace(" ", "").strip() == winner_bin:
                    price_col = col_idx + 1
                    if price_col < len(cells):
                        try:
                            price = _clean_number(cells[price_col])
                            if price > 0:
                                logger.info(
                                    "Тип 3 (выделенная сумма) для БИН %s: %.2f",
                                    winner_bin, price,
                                )
                                return price
                        except (ValueError, IndexError):
                            pass
    return 0.0


# ---------------------------------------------------------------------------
# Шаг 5 — наименование победителя через Subjects API
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
    return (subjects[0].get("nameRu") or "").strip()


# ---------------------------------------------------------------------------
# Задача 2 — Сумма 1-го года (year1_sum)
# ---------------------------------------------------------------------------

def _fetch_lots_for_anno(token: str, ann_id: int) -> list[dict]:
    """
    Запрашивает лоты объявления через GraphQL.
    Возвращает список dict: {id, lotNumber, nameRu}.
    """
    if not ann_id:
        return []
    try:
        resp = _graphql_request(
            token, _LOTS_QUERY,
            {"filter": {"trdBuyId": ann_id}},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Lots(trdBuyId=%s) ошибка: %s", ann_id, exc)
        return []
    return (resp.get("data") or {}).get("Lots") or []


def _fetch_year1_sum(token: str, lot_id: int) -> float:
    """
    Загружает страницу лота goszakup.gov.kz/ru/lots/index/{lot_id}
    и извлекает поле "Сумма 1 год" из таблицы.
    Возвращает 0.0 если поле не найдено или произошла ошибка.
    """
    if not lot_id:
        return 0.0
    url = LOT_PAGE_URL_TEMPLATE.format(lot_id=lot_id)
    try:
        r = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            verify=False,
            timeout=PROTOCOL_TIMEOUT,
        )
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "html.parser")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ошибка загрузки страницы лота id=%s: %s", lot_id, exc)
        return 0.0

    # Ищем строку с "Сумма 1 год" / "1 жыл сомасы" в таблицах страницы
    year1_labels = ["сумма 1 год", "1 жыл сомасы", "сумма на 1 год"]
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            for i, cell in enumerate(cells):
                if cell.lower().strip() in year1_labels and i + 1 < len(cells):
                    try:
                        value = _clean_number(cells[i + 1])
                        if value > 0:
                            logger.info("year1_sum для lot_id=%s: %.2f", lot_id, value)
                            return value
                    except (ValueError, IndexError):
                        pass

    # Также ищем в dl/dt-dd структурах
    for dt in soup.find_all("dt"):
        dt_text = dt.get_text(strip=True).lower()
        if any(label in dt_text for label in year1_labels):
            dd = dt.find_next_sibling("dd")
            if dd:
                try:
                    value = _clean_number(dd.get_text(strip=True))
                    if value > 0:
                        logger.info("year1_sum (dl) для lot_id=%s: %.2f", lot_id, value)
                        return value
                except (ValueError, IndexError):
                    pass

    # Ищем в тексте страницы с regex
    page_text = soup.get_text(" ")
    patterns = [
        r"[Сс]умма\s+1\s+год[а]?\s*[:\—\-]?\s*([\d\s\u00a0\u202f,.]+)",
        r"1\s+жыл\s+сомасы\s*[:\—\-]?\s*([\d\s\u00a0\u202f,.]+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, page_text)
        if m:
            try:
                value = _clean_number(m.group(1).strip().split()[0])
                if value > 0:
                    logger.info("year1_sum (regex) для lot_id=%s: %.2f", lot_id, value)
                    return value
            except (ValueError, IndexError):
                pass

    logger.debug("year1_sum не найдена для lot_id=%s", lot_id)
    return 0.0


def _build_lot_number_to_id_map(lots_api: list[dict]) -> dict[str, int]:
    """Строит маппинг lotNumber → lot_id из ответа API лотов."""
    result = {}
    for lot in lots_api:
        lot_id = _to_int(lot.get("id"))
        lot_number = (lot.get("lotNumber") or "").strip()
        if lot_id and lot_number:
            result[lot_number] = lot_id
    return result


# ---------------------------------------------------------------------------
# Основная функция парсинга
# ---------------------------------------------------------------------------

def scrape_announcements(
    selected_date: str,
    on_progress: ProgressCallback | None = None,
    on_record: RecordCallback | None = None,
    date_to: str | None = None,
    filter_bin: str | None = None,
) -> ScrapeAnnouncementsResult:
    """
    Главная функция парсинга объявлений.
    selected_date — дата "с" (YYYY-MM-DD), date_to — дата "по".
    filter_bin — если задан, в результат попадают только объявления
                 где победителем (в любом лоте) является компания с этим БИН.
    """
    effective_date_to = date_to if date_to else selected_date
    filter_bin = filter_bin.strip() if filter_bin else None
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

    items = _fetch_announcements(token, selected_date, effective_date_to, result.errors)

    if not items:
        logger.warning("Объявления не найдены для диапазона: %s — %s",
                       selected_date, effective_date_to)
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
        count_lots = _to_int(item.get("countLots")) or 1
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
            lots=[],
        )

        try:
            # Шаг 1: договоры
            contracts = _fetch_contracts_for_anno(token, number_anno)
            has_contracts = len(contracts) > 0
            record.has_contracts = has_contracts
            if has_contracts and contracts:
                for c in contracts:
                    biin = (c.get("supplierBiin") or "").strip()
                    if biin:
                        record.winner_bin = biin
                        break

            # Шаг 2: файлы → протокол
            protocol_soup = None
            if ann_id is not None:
                files = _fetch_files_for_anno(token, ann_id)
                protocol_url = None
                for f in files:
                    if "протокол итогов" in (f.get("nameRu") or "").lower():
                        protocol_url = f.get("filePath")
                        break
                if protocol_url:
                    logger.info("Скачиваем протокол для объявления id=%s (лотов: %d)",
                                ann_id, count_lots)
                    protocol_soup = _download_protocol(token, protocol_url)

            # Шаг 3: парсим протокол
            if protocol_soup is not None:
                tables = protocol_soup.find_all("table")

                # Задача 1.2: проверяем не состоялась ли закупка
                if _check_failed_purchase(tables):
                    record.winner_name = "Закупка не состоялась"
                    record.lots = [LotRecord(
                        lot_number="",
                        lot_name=record.name,
                        lot_amount=record.sum_amount,
                        winner_name="Закупка не состоялась",
                        winner_bin="",
                        winner_price=0.0,
                    )]
                elif count_lots > 1:
                    # Многолотовый — парсим каждый лот отдельно
                    lot_records = _parse_protocol_lots(tables)
                    record.lots = lot_records
                    if lot_records:
                        first = lot_records[0]
                        record.winner_name = first.winner_name
                        record.winner_bin = first.winner_bin
                        record.winner_price = first.winner_price
                else:
                    # Однолотовый — стандартный алгоритм
                    proto_name, proto_bin = _find_winner_from_protocol(tables)
                    if proto_name:
                        record.winner_name = proto_name
                    if proto_bin:
                        record.winner_bin = proto_bin

                    # Задача 1.1: fallback — единственный допущенный участник
                    if not record.winner_bin:
                        fallback_name, fallback_bin = _find_single_admitted_supplier(tables)
                        if fallback_bin:
                            record.winner_name = fallback_name
                            record.winner_bin = fallback_bin

                    if record.winner_bin:
                        record.winner_price = _find_winner_price_from_protocol(
                            tables, record.winner_bin
                        )
                    record.lots = [LotRecord(
                        lot_number="",
                        lot_name=record.name,
                        lot_amount=record.sum_amount,
                        winner_name=record.winner_name,
                        winner_bin=record.winner_bin,
                        winner_price=record.winner_price,
                    )]

            # Шаг 4: имя через Subjects API если не нашли
            if record.winner_bin and not record.winner_name:
                record.winner_name = _fetch_subject_name(token, record.winner_bin)
                for lot in record.lots:
                    if lot.winner_bin == record.winner_bin and not lot.winner_name:
                        lot.winner_name = record.winner_name

            # Задача 2: year1_sum — суммы 1-го года для каждого лота
            if ann_id is not None and record.lots:
                lots_api = _fetch_lots_for_anno(token, ann_id)
                if lots_api:
                    lot_num_to_id = _build_lot_number_to_id_map(lots_api)
                    for lot in record.lots:
                        lot_id = None
                        if lot.lot_number:
                            lot_id = lot_num_to_id.get(lot.lot_number)
                        elif len(lots_api) == 1:
                            # Однолотовое объявление — берём единственный лот
                            lot_id = _to_int(lots_api[0].get("id"))
                        if lot_id:
                            lot.year1_sum = _fetch_year1_sum(token, lot_id)

        except Exception as exc:  # noqa: BLE001
            logger.exception("Ошибка обработки объявления id=%s: %s", ann_id, exc)
            record.error = f"Ошибка: {exc}"

        if (not record.winner_bin and not record.winner_name
                and not record.has_contracts and not record.error
                and record.winner_name != "Закупка не состоялась"):
            record.error = "Победитель не найден"

        # Фильтр по БИН
        if filter_bin:
            bin_found = False
            if record.winner_bin == filter_bin:
                bin_found = True
            if not bin_found:
                for lot in record.lots:
                    if lot.winner_bin == filter_bin:
                        bin_found = True
                        break
            if not bin_found:
                logger.debug(
                    "Объявление пропущено — БИН победителя не совпадает с фильтром %s",
                    filter_bin,
                )
                continue

        result.records.append(record)

        if on_record:
            try:
                on_record(record)
            except Exception as exc:  # noqa: BLE001
                logger.warning("on_record callback error: %s", exc)

        if record.error:
            result.errors.append(f"{record.url}: {record.error}")

    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

    date_from = sys.argv[1] if len(sys.argv) > 1 else "2026-04-20"
    date_to   = sys.argv[2] if len(sys.argv) > 2 else "2026-05-21"

    def _progress(current: int, total: int, msg: str) -> None:
        print(f"  [{current}/{total}] {msg}")

    result = scrape_announcements(date_from, on_progress=_progress, date_to=date_to)
    print(f"\n=== Объявления {date_from} — {date_to}: {len(result.records)} найдено ===")
    for rec in result.records[:10]:
        print(f"  №{rec.number} | {rec.name[:50]:50s} | лотов: {len(rec.lots)}")
        for lot in rec.lots:
            print(
                f"    Лот {lot.lot_number}: {lot.winner_name[:30]} | "
                f"{lot.winner_bin} | {lot.winner_price:,.0f} ₸ | "
                f"year1: {lot.year1_sum:,.0f} ₸"
            )
    if result.errors:
        print(f"Всего ошибок: {len(result.errors)}")