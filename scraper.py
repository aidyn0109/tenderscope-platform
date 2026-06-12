"""
scraper.py — Логика парсинга реестра договоров goszakup.gov.kz через GraphQL API v2.

Прямые HTTPS-запросы к https://ows.goszakup.gov.kz/v2/graphql.
Авторизация — Bearer-токен из env GOSZAKUP_TOKEN или st.secrets["goszakup_token"].
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
CONTRACT_UNITS_URL_TEMPLATE = "https://goszakup.gov.kz/ru/egzcontract/cpublic/units/{id}"

# 190 = Действует, 460 = Передан.Действует, 450 = Создано доп.соглашение
TARGET_STATUS_IDS = [190, 460, 450]

# Специфика: год и источники финансирования
SPECIFICS_YEAR = 2026
SPECIFICS_SOURCE_MARKER = "за счет средств местного бюджета"
SPECIFICS_TYPE_MARKER = "строительство новых объектов и реконструкция"
SPECIFICS_VAT_RATE = 1.16  # НДС 16%

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
    specifics_2026_with_vat:    float = 0.0  # Специфика 2026: сумма с НДС
    specifics_2026_without_vat: float = 0.0  # Специфика 2026: сумма без НДС


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
    contract_sum
    fakt_sum
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


def _clean_number(s: str) -> float:
    """Очищает строку с числом (пробелы, неразрывные пробелы, запятые) и конвертирует в float."""
    cleaned = (s.replace("\xa0", "")
                .replace(" ", "")
                .replace("\u202f", "")
                .replace(",", "."))
    # Берём только первое "число" (до пробела или иного разделителя)
    m = re.search(r"[\d.]+", cleaned)
    if m:
        return float(m.group())
    return float(cleaned)


def _fetch_specifics_2026(token: str, contract_id: int) -> float:
    """
    Задача 3: Парсит страницу "Предмет договора"
    goszakup.gov.kz/ru/egzcontract/cpublic/units/{id}

    Алгоритм:
    1. Загружаем страницу /units/{id}
    2. Ищем ссылку по "Ид" (идентификатор предмета договора)
    3. По найденной ссылке загружаем страницу предмета договора
    4. Ищем таблицу "Специфика на утвержденный финансовый год"
    5. Фильтруем строки: источник = "За счет средств местного бюджета"
       И тип = "Строительство новых объектов и реконструкция"
    6. Суммируем значения столбца за год SPECIFICS_YEAR

    Возвращает суммарную сумму с НДС (0.0 если не найдено).
    """
    if not contract_id:
        return 0.0

    units_url = CONTRACT_UNITS_URL_TEMPLATE.format(id=contract_id)
    headers = {"Authorization": f"Bearer {token}"}

    # Шаг 1: загружаем страницу /units/
    try:
        r = requests.get(units_url, headers=headers, verify=False, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "html.parser")
    except Exception as exc:
        logger.warning("Ошибка загрузки /units/%s: %s", contract_id, exc)
        return 0.0

    # Шаг 2: ищем ссылки на предметы договора (по тексту "Ид" или ячейке с числом-ссылкой)
    # Ссылки вида /ru/egzcontract/cpublic/view-subject/{subject_id}
    subject_links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "view-subject" in href or "subject" in href:
            subject_links.append(href)

    # Если нет прямых ссылок view-subject — ищем числа в таблице как ссылки "Ид"
    if not subject_links:
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            # Ищем заголовок с "Ид" или "Идентификатор"
            header_row = rows[0] if rows else None
            if not header_row:
                continue
            header_cells = [th.get_text(strip=True).lower()
                            for th in header_row.find_all(["th", "td"])]
            id_col = None
            for i, h in enumerate(header_cells):
                if h in ("ид", "id", "идентификатор", "ид."):
                    id_col = i
                    break
            if id_col is None:
                continue
            for row in rows[1:]:
                cells = row.find_all(["td", "th"])
                if id_col < len(cells):
                    a_tag = cells[id_col].find("a", href=True)
                    if a_tag:
                        subject_links.append(a_tag["href"])
                    else:
                        # Пробуем взять текст как ID
                        cell_text = cells[id_col].get_text(strip=True)
                        if cell_text.isdigit():
                            subject_links.append(
                                f"/ru/egzcontract/cpublic/view-subject/{cell_text}"
                            )

    if not subject_links:
        logger.debug("Предметы договора не найдены для id=%s", contract_id)
        return 0.0

    # Шаг 3-6: для каждого предмета договора парсим специфику
    total_specifics = 0.0
    base_url = "https://goszakup.gov.kz"

    for link in subject_links:
        if link.startswith("/"):
            subject_url = base_url + link
        elif link.startswith("http"):
            subject_url = link
        else:
            subject_url = base_url + "/" + link

        try:
            r2 = requests.get(subject_url, headers=headers, verify=False,
                              timeout=REQUEST_TIMEOUT)
            r2.raise_for_status()
            soup2 = BeautifulSoup(r2.content, "html.parser")
        except Exception as exc:
            logger.warning("Ошибка загрузки предмета договора %s: %s", subject_url, exc)
            continue

        amount = _parse_specifics_table(soup2)
        total_specifics += amount

    return total_specifics


def _parse_specifics_table(soup: BeautifulSoup) -> float:
    """
    Парсит таблицу "Специфика на утвержденный финансовый год" на странице предмета договора.

    Ищем строки где:
    - Источник финансирования содержит "за счет средств местного бюджета"
    - Тип расходов содержит "строительство новых объектов и реконструкция"

    Возвращает сумму за SPECIFICS_YEAR.
    """
    year_str = str(SPECIFICS_YEAR)
    total = 0.0

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        # Ищем заголовок с упоминанием специфики и финансового года
        header_text = " ".join(
            rows[i].get_text(" ", strip=True).lower()
            for i in range(min(3, len(rows)))
        )
        if ("специфик" not in header_text and "финансов" not in header_text
                and year_str not in header_text):
            continue

        # Определяем индексы нужных столбцов из заголовков
        header_cells = [td.get_text(strip=True).lower()
                        for td in rows[0].find_all(["th", "td"])]
        # Для многострочных заголовков объединяем первые 2-3 строки
        if len(rows) > 1:
            header_cells2 = [td.get_text(strip=True).lower()
                             for td in rows[1].find_all(["th", "td"])]
        else:
            header_cells2 = []

        # Ищем индексы: источник финансирования, тип расходов, год
        source_col = None
        type_col = None
        year_col = None

        all_header_cells = header_cells + header_cells2

        for i, h in enumerate(header_cells):
            if "источник" in h or "financing" in h:
                source_col = i
            if "тип" in h or "вид" in h or "расход" in h:
                type_col = i
            if year_str in h:
                year_col = i

        # Если год в подзаголовках (2-я строка заголовка)
        if year_col is None and header_cells2:
            for i, h in enumerate(header_cells2):
                if year_str in h:
                    year_col = i
                    break

        if year_col is None:
            # Год не найден в заголовке — ищем как текст в любой ячейке таблицы
            for row in rows:
                for col_i, td in enumerate(row.find_all(["td", "th"])):
                    if year_str in td.get_text(strip=True):
                        year_col = col_i
                        break
                if year_col is not None:
                    break

        if year_col is None:
            continue  # Нет столбца с нужным годом — это не наша таблица

        # Проходим по строкам данных
        for row in rows[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            if not cells:
                continue

            row_text = " ".join(cells).lower()

            # Проверяем наличие маркеров источника и типа
            has_source = SPECIFICS_SOURCE_MARKER in row_text
            has_type = SPECIFICS_TYPE_MARKER in row_text

            # Если маркеры в отдельных столбцах — проверяем их
            if source_col is not None and type_col is not None:
                source_text = cells[source_col].lower() if source_col < len(cells) else ""
                type_text = cells[type_col].lower() if type_col < len(cells) else ""
                has_source = SPECIFICS_SOURCE_MARKER in source_text
                has_type = SPECIFICS_TYPE_MARKER in type_text

            if not (has_source and has_type):
                continue

            # Берём значение из столбца года
            if year_col < len(cells):
                try:
                    amount = _clean_number(cells[year_col])
                    if amount > 0:
                        total += amount
                        logger.info(
                            "Специфика %d: %.2f ₸ (источник: местный бюджет, строительство)",
                            SPECIFICS_YEAR, amount,
                        )
                except (ValueError, IndexError):
                    pass

    return total


def _contract_record_from_item(item: dict, bin_number: str) -> ContractRecord:
    cid = item.get("id")
    amount_final = _to_float(item.get("contract_sum"))
    amount_actual = _to_float(item.get("fakt_sum"))
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

        # Задача 3: парсим специфику на 2026 год
        cid = item.get("id")
        if cid is not None:
            try:
                with_vat = _fetch_specifics_2026(token, int(cid))
                record.specifics_2026_with_vat = with_vat
                record.specifics_2026_without_vat = (
                    round(with_vat / SPECIFICS_VAT_RATE, 2) if with_vat > 0 else 0.0
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Ошибка парсинга специфики id=%s: %s", cid, exc)

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