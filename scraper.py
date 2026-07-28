"""
scraper.py — Логика парсинга реестра договоров goszakup.gov.kz через GraphQL API v2 + v3.

Фильтры (соответствуют URL реестра):
  - supplier_biin — БИН поставщика
  - ref_contract_status_id: [190, 460, 450] (Действует / Передан.Действует / Доп.соглашение)
  - ref_subject_type_id: 2 (Работа) — фильтруется в Python
  - crdate: 2026 год — фильтруется в Python

Для каждого договора извлекаются данные из раздела «Предметы договора»:
  Алгоритм выбора unit:
    1. Из всех contract_units выбираем unit с минимальным item_price (> 0)
    2. Если fact_sum > 0 → Сценарий 1 (обычный):
       - Сумма 1 = item_price (Сумма по предмету договора без НДС)
       - Сумма 2 = fact_sum (Сумма исполненная, фактическая)
    3. Если fact_sum == 0 → Сценарий 2 (v3 ContractSpecSum):
       - Сумма 1 = planSum за текущий год (Утвержденная планируемая сумма)
       - Сумма 2 = factSum за текущий год
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

GRAPHQL_V2 = "https://ows.goszakup.gov.kz/v2/graphql"
GRAPHQL_V3 = "https://ows.goszakup.gov.kz/v3/graphql"
REQUEST_TIMEOUT = 60
RETRY_COUNT = 2
RETRY_DELAY = 3
PAGE_LIMIT = 50
MAX_RECORDS = 10_000

CONTRACT_URL_TEMPLATE = "https://goszakup.gov.kz/ru/egzcontract/cpublic/show/{id}"

TARGET_STATUS_IDS = [190, 460, 450]
TARGET_YEAR = 2026
TARGET_SUBJECT_TYPE_ID = 2  # Работа

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Типы данных
# ---------------------------------------------------------------------------

@dataclass
class ContractRecord:
    """Одна запись о договоре."""
    bin: str
    supplier_name: str
    contract_number: str
    description: str
    cr_datetime: str
    amount_planned: float       # Сумма 1
    amount_actual: float        # Сумма 2
    amount_total: float         # Сумма1 − Сумма2
    max_income: float
    url: str
    error: str = ""


@dataclass
class ScrapeResult:
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

# v2: список договоров + contract_units
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

# v2: subjects API
_SUBJECT_QUERY = """
query($f: SubjectFiltersInput) {
  subjects(filters: $f) {
    bin
    name_ru
  }
}
"""

# v3: ContractSpecSum для unit (сценарий 2)
_SPECSUM_QUERY = """
query($f: ObContractFiltersInput) {
  ObContract(limit: 1, filter: $f) {
    id
    ContractSpecSum {
      id
      unitId
      finYear
      planSum
      factSum
    }
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
                endpoint,
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


def _v2_request(token: str, query: str, variables: dict) -> dict:
    return _graphql_request(GRAPHQL_V2, token, query, variables)


def _v3_request(token: str, query: str, variables: dict) -> dict:
    return _graphql_request(GRAPHQL_V3, token, query, variables)


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


def _sum_units(units: list[dict], field: str) -> float:
    return sum(_to_float(u.get(field)) for u in units if u.get(field) is not None)


# ---------------------------------------------------------------------------
# Кэш имён поставщиков
# ---------------------------------------------------------------------------

_supplier_name_cache: dict[str, str] = {}


def _fetch_supplier_name(token: str, bin_number: str) -> str:
    if bin_number in _supplier_name_cache:
        return _supplier_name_cache[bin_number]
    try:
        resp = _v2_request(token, _SUBJECT_QUERY, {"f": {"bin": bin_number}})
        subjects = (resp.get("data") or {}).get("subjects") or []
        if subjects:
            name = (subjects[0].get("name_ru") or "").strip()
            _supplier_name_cache[bin_number] = name
            logger.info("Поставщик %s: %s", bin_number, name)
            return name
    except Exception as exc:
        logger.warning("Ошибка subjects API для %s: %s", bin_number, exc)
    _supplier_name_cache[bin_number] = ""
    return ""


# ---------------------------------------------------------------------------
# Выбор правильного unit и расчёт сумм
# ---------------------------------------------------------------------------

def _pick_best_unit(units: list[dict]) -> dict | None:
    """
    Из всех contract_units выбирает правильный unit:
    — с минимальным item_price (> 0), так как основной предмет договора
      имеет меньшую стоимость чем общая сумма контракта.
    """
    positive = [u for u in units if _to_float(u.get("item_price")) > 0]
    if not positive:
        return units[0] if units else None
    return min(positive, key=lambda u: _to_float(u.get("item_price")))


def _calc_amounts(token: str, contract_id: int, units: list[dict]) -> tuple[float, float]:
    """
    Возвращает (amount_planned, amount_actual) для контракта.
    Сценарий 1: unit.fact_sum > 0 → item_price / fact_sum
    Сценарий 2: unit.fact_sum == 0 → ContractSpecSum planSum/factSum за TARGET_YEAR
    """
    unit = _pick_best_unit(units)
    if not unit:
        return 0.0, 0.0

    fact_sum = _to_float(unit.get("fact_sum"))
    item_price = _to_float(unit.get("item_price"))

    if fact_sum > 0:
        # Сценарий 1: обычная таблица view-subject
        return item_price, fact_sum

    # Сценарий 2: нужно через v3 ContractSpecSum
    unit_id = unit.get("id")
    if not unit_id or not contract_id:
        logger.warning("Нет unit_id или contract_id для сценария 2")
        return item_price, fact_sum

    try:
        resp = _v3_request(token, _SPECSUM_QUERY, {"f": {"id": [contract_id]}})
        ob = ((resp.get("data") or {}).get("ObContract") or [None])[0]
        if not ob:
            return item_price, fact_sum

        spec_sums = ob.get("ContractSpecSum") or []
        # Фильтруем: unitId совпадает + finYear == TARGET_YEAR
        year_specs = [
            ss for ss in spec_sums
            if ss.get("unitId") == unit_id and ss.get("finYear") == TARGET_YEAR
        ]
        if year_specs:
            ss = year_specs[0]
            plan_sum = _to_float(ss.get("planSum"))
            spec_fact = _to_float(ss.get("factSum"))
            logger.info(
                "Сценарий 2 (ContractSpecSum): unit=%d, year=%d, planSum=%.2f, factSum=%.2f",
                unit_id, TARGET_YEAR, plan_sum, spec_fact,
            )
            return plan_sum, spec_fact

        # Fallback: если нет за текущий год — суммируем все года для этого unit
        all_for_unit = [ss for ss in spec_sums if ss.get("unitId") == unit_id]
        if all_for_unit:
            total_plan = sum(_to_float(ss.get("planSum")) for ss in all_for_unit)
            total_fact = sum(_to_float(ss.get("factSum")) for ss in all_for_unit)
            logger.info("Сценарий 2 (fallback): unit=%d, total planSum=%.2f", unit_id, total_plan)
            return total_plan, total_fact

    except Exception as exc:
        logger.warning("Ошибка v3 ContractSpecSum для contract=%d: %s", contract_id, exc)

    return item_price, fact_sum


# ---------------------------------------------------------------------------
# Сбор договоров для БИН
# ---------------------------------------------------------------------------

def _fetch_contracts_for_bin(token: str, bin_number: str, errors_sink: list[str]) -> list[dict]:
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
            response = _v2_request(token, _CONTRACT_QUERY, variables)
        except Exception as exc:
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

    filtered = []
    for item in items:
        if item.get("ref_subject_type_id") != TARGET_SUBJECT_TYPE_ID:
            continue
        crdate = str(item.get("crdate") or "")
        if not crdate.startswith(str(TARGET_YEAR)):
            continue
        filtered.append(item)

    logger.info("БИН %s: всего %d, после фильтрации — %d", bin_number, len(items), len(filtered))
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
    result = ScrapeResult(bin=bin_number, max_income=max_income)
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
            except Exception:
                pass

        cid = item.get("id")
        crdate = (item.get("crdate") or "").strip()
        description = (item.get("description_ru") or "").strip() or "(описание отсутствует)"
        contract_number = (item.get("contract_number_sys") or "").strip()
        url = CONTRACT_URL_TEMPLATE.format(id=cid) if cid is not None else ""

        units = item.get("contract_units") or []
        amount_planned, amount_actual = _calc_amounts(token, cid, units)
        amount_total = round(amount_planned - amount_actual, 2)

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
            except Exception:
                pass

    return result


def scrape_all(
    bin_data: list[dict],
    on_bin_start: Callable[[int, int, str], None] | None = None,
    on_contract_progress: ProgressCallback | None = None,
    on_record: RecordCallback | None = None,
) -> list[ScrapeResult]:
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
            except Exception:
                pass

        logger.info("=== Обработка БИН %s (%d/%d) ===", bin_number, i, total_bins)
        result = scrape_bin(bin_number, max_income, token,
                            on_contract_progress=on_contract_progress,
                            on_record=on_record)
        results.append(result)

    return results


# ---------------------------------------------------------------------------
# Тест
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
            print(f"  Компания: {rec.supplier_name or '—'} | №{rec.contract_number} | "
                  f"план: {rec.amount_planned:,.2f} | факт: {rec.amount_actual:,.2f} | "
                  f"итого: {rec.amount_total:,.2f} ₸")
        if res.errors:
            print(f"  Ошибок: {len(res.errors)}")