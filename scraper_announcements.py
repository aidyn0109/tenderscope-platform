"""
scraper_announcements.py — Парсинг объявлений через GraphQL API v3.

Новая логика:
  - Фильтры: refBuyStatusId=[350] (Договор подписан), refTradeMethodsId=[2,32,188,201],
    itogiDatePublic в диапазоне дат (Python)
  - Пользователь вводит БИН + диапазон дат протокола
  - Для каждого объявления:
    1. Проверяем наличие договора (v3 Contract API по trdBuyId)
    2. Если договор ЕСТЬ → пропускаем
    3. Если договора НЕТ:
       a. Ищем БИН среди победителей лотов (парсинг HTML протокола)
       b. Если БИН найден → загружаем страницу лота для "Сумма 1 год"
       c. Берём ссылку на протокол итогов из Files
  - Результат: БИН, Наименование компании, Номер объявления, Сумма 1 год, Ссылка на протокол
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
    import streamlit as st
except Exception:
    st = None

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

GRAPHQL_V3 = "https://ows.goszakup.gov.kz/v3/graphql"
REQUEST_TIMEOUT = 60
PROTOCOL_TIMEOUT = 30
RETRY_COUNT = 2
RETRY_DELAY = 3
MAX_RECORDS = 10_000

LOT_PAGE_URL_TEMPLATE = "https://goszakup.gov.kz/ru/lots/index/{lot_id}"
ANNOUNCEMENT_URL_TEMPLATE = "https://goszakup.gov.kz/ru/announce/index/{id}"

# Новые фильтры: статус 350 (Договор подписан), способы: Аукцион(2), 32, Конкурс(188), Конкурс с предквал.(201)
TARGET_STATUS_IDS = [350]
TARGET_METHOD_IDS = [2, 32, 188, 201]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Типы данных
# ---------------------------------------------------------------------------

@dataclass
class AnnouncementRecord:
    """Одна запись об объявлении с нужным БИН."""
    bin: str                    # БИН компании
    supplier_name: str          # Наименование компании
    announcement_number: str    # Номер объявления (numberAnno)
    announcement_name: str      # Наименование объявления
    year1_sum: float            # Сумма 1 год (со страницы лота)
    protocol_url: str           # Ссылка на протокол итогов
    announcement_url: str       # Ссылка на объявление
    error: str = ""


@dataclass
class ScrapeAnnouncementsResult:
    results: list[AnnouncementRecord] = field(default_factory=list)
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
            secret = st.secrets.get("goszakup_token")
            if secret:
                secret = str(secret).strip()
                if secret:
                    return secret
        except Exception:
            pass
    raise RuntimeError("Токен Goszakup API не найден.")


# ---------------------------------------------------------------------------
# GraphQL запросы
# ---------------------------------------------------------------------------

_TRD_BUY_QUERY = """
query($filter: TrdBuyFiltersInput, $after: Int) {
  TrdBuy(limit: 50, filter: $filter, after: $after) {
    id
    numberAnno
    nameRu
    refBuyStatusId
    refTradeMethodsId
    itogiDatePublic
    countLots
    totalSum
  }
}
"""

_FILES_QUERY = """
query($filter: TrdBuyFiltersInput) {
  TrdBuy(limit: 1, filter: $filter) {
    id
    Files {
      id
      nameRu
      filePath
    }
  }
}
"""

# v3 Contract для проверки наличия договора
_CONTRACT_CHECK_QUERY = """
query($filter: ContractFiltersInput) {
  Contract(limit: 1, filter: $filter) {
    id
  }
}
"""

# v3 Lots — получение списка лотов объявления
_LOTS_LIST_QUERY = """
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
                GRAPHQL_V3,
                json=payload,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
                verify=False,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            last_exc = exc
            logger.warning("GraphQL попытка %d/%d: %s", attempt + 1, RETRY_COUNT + 1, exc)
            if attempt < RETRY_COUNT:
                time.sleep(RETRY_DELAY)
    raise RuntimeError(f"GraphQL ошибка: {last_exc}")


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

def _to_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _clean_number(s: str) -> float:
    cleaned = (s.replace("\xa0", "")
                .replace(" ", "")
                .replace("\u202f", "")
                .replace(",", "."))
    m = re.search(r"[\d.]+", cleaned)
    if m:
        return float(m.group())
    return float(cleaned)


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
# Получение наименования компании (v3 Subjects API)
# ---------------------------------------------------------------------------

_NAME_CACHE: dict[str, str] = {}


def _fetch_supplier_name(token: str, bin_number: str) -> str:
    if bin_number in _NAME_CACHE:
        return _NAME_CACHE[bin_number]
    try:
        query = """
        query($filter: TrdBuyFiltersInput) {
          Subjects(limit: 1, filter: $filter) {
            bin
            nameRu
          }
        }
        """
        resp = _graphql_request(token, query, {"filter": {"bin": bin_number}})
        subjects = (resp.get("data") or {}).get("Subjects") or []
        if subjects:
            name = (subjects[0].get("nameRu") or "").strip()
            _NAME_CACHE[bin_number] = name
            return name
    except Exception as exc:
        logger.warning("Ошибка Subjects для БИН %s: %s", bin_number, exc)
    _NAME_CACHE[bin_number] = ""
    return ""


# ---------------------------------------------------------------------------
# Проверка наличия договора
# ---------------------------------------------------------------------------

def _has_contract(token: str, trd_buy_number_anno: str) -> bool:
    """Проверяет, есть ли договор для объявления."""
    if not trd_buy_number_anno:
        return False
    try:
        resp = _graphql_request(
            token, _CONTRACT_CHECK_QUERY,
            {"filter": {"trdBuyNumberAnno": trd_buy_number_anno}},
        )
        contracts = (resp.get("data") or {}).get("Contract") or []
        return len(contracts) > 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Поиск БИН в протоколе итогов
# ---------------------------------------------------------------------------

def _get_protocol_url(token: str, ann_id: int) -> str | None:
    """Получает ссылку на протокол итогов из Files объявления."""
    if not ann_id:
        return None
    try:
        resp = _graphql_request(token, _FILES_QUERY, {"filter": {"id": [ann_id]}})
        items = (resp.get("data") or {}).get("TrdBuy") or []
        if not items:
            return None
        files = items[0].get("Files") or []
        for f in files:
            if "протокол итогов" in (f.get("nameRu") or "").lower():
                return f.get("filePath")
    except Exception as exc:
        logger.warning("Ошибка Files для id=%s: %s", ann_id, exc)
    return None


def _download_protocol(token: str, url: str) -> BeautifulSoup | None:
    """Скачивает HTML протокола итогов."""
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
    except Exception as exc:
        logger.warning("Ошибка скачивания протокола %s: %s", url, exc)
        return None


def _find_bin_in_protocol(soup: BeautifulSoup, target_bin: str) -> bool:
    """Ищет БИН в HTML-протоколе итогов (в любых таблицах)."""
    if not soup or not target_bin:
        return False
    tables = soup.find_all("table")
    for table in tables:
        rows = table.find_all("tr")
        for row in rows:
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            for cell in cells:
                cleaned = cell.replace(" ", "").replace("\xa0", "")
                if cleaned == target_bin:
                    logger.info("БИН %s найден в протоколе", target_bin)
                    return True
    return False


# ---------------------------------------------------------------------------
# Сумма 1 год со страницы лота
# ---------------------------------------------------------------------------

def _fetch_year1_sum(token: str, lot_id: int) -> float:
    """Загружает страницу лота и извлекает 'Сумма 1 год'."""
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
    except Exception as exc:
        logger.warning("Ошибка загрузки страницы лота id=%s: %s", lot_id, exc)
        return 0.0

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


# ---------------------------------------------------------------------------
# Выборка объявлений (шаг 1)
# ---------------------------------------------------------------------------

def _fetch_announcements(
    token: str,
    date_from: str,
    date_to: str,
    errors_sink: list[str],
) -> list[dict]:
    """Загружает объявления с фильтрами через GraphQL v3."""
    items: list[dict] = []
    after: int | None = None
    filter_input = {
        "refBuyStatusId": TARGET_STATUS_IDS,
        "refTradeMethodsId": TARGET_METHOD_IDS,
    }

    while True:
        variables: dict = {"filter": filter_input}
        if after is not None:
            variables["after"] = after
        try:
            response = _graphql_request(token, _TRD_BUY_QUERY, variables)
        except Exception as exc:
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

    # Фильтрация по дате в Python
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
# Получение лотов объявления
# ---------------------------------------------------------------------------

def _fetch_lots_for_anno(token: str, ann_id: int) -> list[dict]:
    """Получает список лотов объявления."""
    if not ann_id:
        return []
    try:
        resp = _graphql_request(
            token, _LOTS_LIST_QUERY,
            {"filter": {"trdBuyId": ann_id}},
        )
    except Exception as exc:
        logger.warning("Lots(trdBuyId=%s) ошибка: %s", ann_id, exc)
        return []
    return (resp.get("data") or {}).get("Lots") or []


# ---------------------------------------------------------------------------
# Основная функция парсинга
# ---------------------------------------------------------------------------

def scrape_announcements(
    date_from: str,
    date_to: str,
    filter_bin: str,
    on_progress: ProgressCallback | None = None,
    on_record: RecordCallback | None = None,
) -> ScrapeAnnouncementsResult:
    """
    Главная функция парсинга объявлений.
    date_from, date_to — диапазон дат протокола итогов (YYYY-MM-DD).
    filter_bin — БИН компании для поиска.
    """
    _NAME_CACHE.clear()
    result = ScrapeAnnouncementsResult()

    if not filter_bin:
        result.errors.append("Не задан БИН для поиска")
        return result

    try:
        token = _get_token()
    except Exception as exc:
        result.errors.append(str(exc))
        return result

    # Шаг 1: список объявлений с фильтрами
    if on_progress:
        try:
            on_progress(0, 0, "Запрос списка объявлений...")
        except Exception:
            pass

    items = _fetch_announcements(token, date_from, date_to, result.errors)

    if not items:
        logger.warning("Объявления не найдены для диапазона: %s — %s", date_from, date_to)
        return result

    total = len(items)
    for idx, item in enumerate(items, start=1):
        if on_progress:
            try:
                on_progress(idx, total, f"Объявление {idx} из {total}")
            except Exception:
                pass

        ann_id = _to_int(item.get("id"))
        number_anno = (item.get("numberAnno") or "").strip()
        name_ru = (item.get("nameRu") or "").strip()

        logger.info("Объявление #%s: %s", number_anno, name_ru[:80])

        # Шаг 2: проверяем наличие договора
        has_contract = _has_contract(token, number_anno)
        if has_contract:
            logger.info("  → договор найден, пропускаем")
            continue

        logger.info("  → договора нет, проверяем БИН в протоколе")

        # Шаг 3: получаем протокол итогов
        protocol_url = _get_protocol_url(token, ann_id)
        if not protocol_url:
            logger.info("  → протокол не найден")
            continue

        protocol_soup = _download_protocol(token, protocol_url)
        if not protocol_soup:
            continue

        # Шаг 4: ищем БИН в протоколе
        bin_found = _find_bin_in_protocol(protocol_soup, filter_bin)
        if not bin_found:
            logger.debug("  → БИН %s не найден в протоколе", filter_bin)
            continue

        logger.info("  → БИН %s найден!", filter_bin)

        # Шаг 5: получаем лоты и сумму 1 год
        lots = _fetch_lots_for_anno(token, ann_id)
        year1_sum = 0.0
        if lots:
            # Берём первый лот
            lot_id = _to_int(lots[0].get("id"))
            if lot_id:
                year1_sum = _fetch_year1_sum(token, lot_id)
                logger.info("  → Сумма 1 год: %.2f", year1_sum)

        # Шаг 6: наименование компании
        supplier_name = _fetch_supplier_name(token, filter_bin)

        record = AnnouncementRecord(
            bin=filter_bin,
            supplier_name=supplier_name,
            announcement_number=number_anno,
            announcement_name=name_ru,
            year1_sum=year1_sum,
            protocol_url=protocol_url or "",
            announcement_url=ANNOUNCEMENT_URL_TEMPLATE.format(id=ann_id) if ann_id else "",
        )
        result.results.append(record)

        if on_record:
            try:
                on_record(record)
            except Exception:
                pass

    return result


# ---------------------------------------------------------------------------
# Тест
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

    date_from = sys.argv[1] if len(sys.argv) > 1 else "2026-07-04"
    date_to = sys.argv[2] if len(sys.argv) > 2 else "2026-07-28"
    test_bin = sys.argv[3] if len(sys.argv) > 3 else "031240001439"

    def _progress(cur: int, tot: int, msg: str) -> None:
        print(f"  [{cur}/{tot}] {msg}")

    res = scrape_announcements(date_from, date_to, test_bin, on_progress=_progress)
    print(f"\n=== Объявления {date_from} — {date_to}, БИН {test_bin}: {len(res.results)} шт. ===")
    for r in res.results:
        print(f"  БИН: {r.bin} | Компания: {r.supplier_name} | №{r.announcement_number}")
        print(f"  Сумма 1 год: {r.year1_sum:,.2f} ₸ | Протокол: {r.protocol_url}")
    if res.errors:
        print(f"Ошибок: {len(res.errors)}")