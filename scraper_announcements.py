"""
scraper_announcements.py — Парсинг закупочных объявлений через GraphQL API v3.

Все данные тянутся прямыми HTTPS-запросами к
https://ows.goszakup.gov.kz/v3/graphql.
Победитель и цена извлекаются из HTML-протокола итогов,
который скачивается через API по ссылке из Files объявления.
"""

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable

import requests
import urllib3
from bs4 import BeautifulSoup

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
PROTOCOL_TIMEOUT = 30
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

    # Фильтрация по дате — берём объявления где endDate >= selected_date
    filtered = [
        r for r in items
        if r.get("endDate") and str(r["endDate"])[:10] >= selected_date
    ]
    logger.info("TrdBuy: получено %d записей, после фильтра endDate >= %s — %d",
                len(items), selected_date, len(filtered))
    return filtered


# ---------------------------------------------------------------------------
# Шаг 2 — наличие договоров по номеру объявления
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
# Шаг 3 — файлы объявления (протокол итогов)
# ---------------------------------------------------------------------------

def _fetch_files_for_anno(token: str, ann_id: int) -> list[dict]:
    """Получает список файлов объявления через GraphQL."""
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
    """Скачивает HTML протокол и возвращает BeautifulSoup объект. При ошибке — None."""
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
# Шаг 4 — парсинг HTML протокола: победитель и цена
# ---------------------------------------------------------------------------

def _find_winner_from_protocol(tables) -> tuple[str, str]:
    """
    Ищет таблицу победителя в HTML протоколе.
    Возвращает (winner_name, winner_bin).
    Таблица победителя содержит 'победител' или 'жеңімпаз' в заголовке.
    Победитель — первая строка данных: cells[1]=название, cells[2]=БИН.
    """
    for table in tables:
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        header_text = rows[0].get_text().lower()
        if "победител" not in header_text and "жеңімпаз" not in header_text:
            continue
        # Перебираем строки данных (пропускаем нумерацию "1 | 2 | 3 | ...")
        for row in rows[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            if len(cells) < 3:
                continue
            # Пропускаем строку-нумерацию "1 | 2 | 3 | ..."
            if cells[0] in ("1", "№") and cells[1] in ("2", "Наименование", "Атауы"):
                continue
            name = cells[1].strip()
            bin_val = cells[2].strip()
            # БИН должен быть 12 цифр
            if name and bin_val and bin_val.replace(" ", "").isdigit():
                bin_clean = bin_val.replace(" ", "")
                if len(bin_clean) == 12:
                    return name, bin_clean
    return "", ""


def _find_winner_price_from_protocol(tables, winner_bin: str) -> float:
    """
    Ищет цену победителя в таблице расчёта условных цен.
    Заголовок таблицы содержит 'цена поставщика' или 'өнім берушінің бағасы'.
    Находит строку с БИН победителя, берёт столбец 'Цена поставщика' (индекс 4).
    """
    if not winner_bin:
        return 0.0

    for table in tables:
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        header_text = rows[0].get_text().lower()
        if ("цена поставщика" not in header_text
                and "өнім берушінің бағасы" not in header_text):
            continue
        for row in rows[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            if len(cells) < 5:
                continue
            # Пропускаем строку-нумерацию
            if cells[0] in ("1", "№") and cells[1] in ("2", "Наименование", "Атауы"):
                continue
            # БИН в столбце 2 (индекс 2)
            bin_val = cells[2].replace(" ", "").strip()
            if bin_val == winner_bin:
                try:
                    # Цена поставщика в столбце 4 (индекс 4)
                    price_str = cells[4].replace(" ", "").replace("\xa0", "").replace(",", ".")
                    price = float(price_str)
                    if price > 0:
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
            # ── Шаг 1: проверяем наличие договоров ──────────────────────────
            contracts = _fetch_contracts_for_anno(token, number_anno)
            has_contracts = len(contracts) > 0
            record.has_contracts = has_contracts

            if has_contracts:
                # Берём БИН из первого договора с непустым supplierBiin
                for c in contracts:
                    biin = (c.get("supplierBiin") or "").strip()
                    if biin:
                        record.winner_bin = biin
                        break

            # ── Шаг 2: получаем файлы объявления и ищем протокол итогов ────
            protocol_soup = None
            if ann_id is not None:
                files = _fetch_files_for_anno(token, ann_id)
                protocol_url = None
                for f in files:
                    file_name = (f.get("nameRu") or "").lower()
                    if "протокол итогов" in file_name:
                        protocol_url = f.get("filePath")
                        break

                if protocol_url:
                    logger.info("Скачиваем протокол для объявления id=%s", ann_id)
                    protocol_soup = _download_protocol(token, protocol_url)

            # ── Шаг 3: парсим протокол — победитель и цена ──────────────────
            if protocol_soup is not None:
                tables = protocol_soup.find_all("table")

                # Победитель из протокола
                proto_name, proto_bin = _find_winner_from_protocol(tables)
                if proto_name:
                    record.winner_name = proto_name
                if proto_bin:
                    # Протокол приоритетнее договора для БИН
                    record.winner_bin = proto_bin

                # Цена только если нет договоров
                if not has_contracts and record.winner_bin:
                    record.winner_price = _find_winner_price_from_protocol(
                        tables, record.winner_bin
                    )

            # ── Шаг 4: если имя ещё не найдено — пробуем через Subjects API ─
            if record.winner_bin and not record.winner_name:
                record.winner_name = _fetch_subject_name(token, record.winner_bin)

        except Exception as exc:  # noqa: BLE001
            logger.exception("Ошибка обработки объявления id=%s: %s", ann_id, exc)
            record.error = f"Ошибка: {exc}"

        # Помечаем если победитель не найден совсем
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

    test_date = sys.argv[1] if len(sys.argv) > 1 else "2026-05-01"

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