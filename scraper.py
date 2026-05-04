"""
scraper.py — Логика парсинга goszakup.gov.kz
Использует синхронный Playwright API (sync_playwright).
"""

import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    sync_playwright,
)

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

PLAYWRIGHT_CONFIG = {
    "headless": True,
    "timeout": 90_000,
    "viewport": {"width": 1280, "height": 800},
    "locale": "ru-RU",
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

BASE_URL     = "https://goszakup.gov.kz"
REGISTRY_URL = f"{BASE_URL}/ru/registry/contract"

# 190 = Действует, 460 = Передан.Действует, 450 = Создано доп.соглашение
TARGET_STATUS_VALUES = ["190", "460", "450"]

# Ресурсы, которые блокируем — не нужны для парсинга, экономят время
BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}

REQUEST_DELAY = 0.3   # задержка между договорами (сек)
RETRY_COUNT   = 2

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

PAGE_RECYCLE_INTERVAL = 5  # Переоткрываем страницу каждые N договоров → освобождаем память


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _parse_amount(raw: str) -> float:
    if not raw:
        return 0.0
    cleaned = re.sub(r"[^\d,\.]", "", raw.replace("\xa0", "").replace(" ", ""))
    cleaned = cleaned.replace(",", ".")
    parts = cleaned.split(".")
    if len(parts) > 2:
        cleaned = "".join(parts[:-1]) + "." + parts[-1]
    try:
        return float(cleaned)
    except ValueError:
        logger.warning("Не удалось распарсить сумму: %r", raw)
        return 0.0


def _random_delay(base: float = REQUEST_DELAY) -> None:
    """Случайная задержка ±30% от base для имитации человека."""
    time.sleep(base * random.uniform(0.7, 1.3))


def _setup_page_routes(page: Page) -> None:
    """Блокирует ненужные ресурсы для ускорения загрузки."""
    page.route(
        "**/*",
        lambda route: (
            route.abort()
            if route.request.resource_type in BLOCKED_RESOURCE_TYPES
            else route.continue_()
        ),
    )


# ---------------------------------------------------------------------------
# Навигация к реестру договоров
# ---------------------------------------------------------------------------

def navigate_to_registry(page: Page) -> None:
    logger.info("Переходим на реестр договоров: %s", REGISTRY_URL)
    _setup_page_routes(page)
    for attempt in range(1, 4):
        try:
            page.goto(REGISTRY_URL, wait_until="domcontentloaded",
                      timeout=PLAYWRIGHT_CONFIG["timeout"])
            page.wait_for_timeout(600)
            logger.info("Реестр загружен (попытка %d). URL: %s", attempt, page.url)
            return
        except Exception as exc:
            logger.warning("navigate_to_registry попытка %d/3: %s", attempt, exc)
            if attempt == 3:
                raise
            logger.info("Повтор через 5 сек...")
            time.sleep(5)


# ---------------------------------------------------------------------------
# Применение фильтров
# ---------------------------------------------------------------------------

def apply_filters(page: Page, bin_number: str) -> None:
    logger.info("Применяем фильтры для БИН: %s", bin_number)

    supplier_input = page.locator("#in_supplier")
    supplier_input.wait_for(state="visible", timeout=PLAYWRIGHT_CONFIG["timeout"])
    supplier_input.fill(bin_number)
    page.wait_for_timeout(150)

    page.evaluate(
        """(values) => {
            const sel = document.querySelector("select[name='filter[status][]']");
            if (!sel) return;
            if (window.jQuery) {
                jQuery(sel).val(values).trigger('change');
            } else {
                Array.from(sel.options).forEach(opt => {
                    opt.selected = values.includes(opt.value);
                });
            }
        }""",
        TARGET_STATUS_VALUES,
    )
    page.wait_for_timeout(200)

    search_btn = page.locator("button[type='submit']").first
    search_btn.wait_for(state="visible", timeout=PLAYWRIGHT_CONFIG["timeout"])
    search_btn.click()
    page.wait_for_load_state("domcontentloaded", timeout=PLAYWRIGHT_CONFIG["timeout"])
    page.wait_for_timeout(600)

    logger.info("Фильтры применены. URL: %s", page.url)


# ---------------------------------------------------------------------------
# Сбор ссылок на договоры (с пагинацией)
# ---------------------------------------------------------------------------

def collect_contract_links(page: Page) -> list[str]:
    links: list[str] = []
    page_num = 1

    while True:
        logger.info("Сбор ссылок — страница %d", page_num)

        anchors  = page.locator("a[href*='/egzcontract/cpublic/show/']")
        count    = anchors.count()
        new_count = 0

        for i in range(count):
            href = anchors.nth(i).get_attribute("href")
            if href:
                absolute = href if href.startswith("http") else BASE_URL + href
                if absolute not in links:
                    links.append(absolute)
                    new_count += 1

        logger.info("Страница %d: %d новых ссылок (всего: %d)", page_num, new_count, len(links))

        if new_count == 0:
            break

        next_li = page.locator(".pagination li").filter(has_text="»").first
        if not next_li.count():
            break

        is_disabled = next_li.evaluate(
            "el => el.classList.contains('disabled') || el.classList.contains('active')"
        )
        if is_disabled:
            break

        first_href_before = links[-(new_count)] if new_count else None

        next_li.locator("a").first.click()
        page.wait_for_load_state("domcontentloaded", timeout=PLAYWRIGHT_CONFIG["timeout"])
        page.wait_for_timeout(500)

        first_anchor = page.locator("a[href*='/egzcontract/cpublic/show/']").first
        if first_anchor.count():
            first_href_after = first_anchor.get_attribute("href")
            if first_href_after and (
                first_href_after == first_href_before
                or first_href_after in links
            ):
                break
        else:
            break

        page_num += 1
        if page_num > 100:
            break

        _random_delay(0.3)

    logger.info("Всего ссылок: %d", len(links))
    return links


# ---------------------------------------------------------------------------
# Парсинг одного договора
# ---------------------------------------------------------------------------

# Все нужные поля в одном JS-запросе — единственный обход DOM вместо 5 отдельных
_EXTRACT_JS = """(labels) => {
    const result = {};
    for (const td of document.querySelectorAll("td")) {
        const text = td.innerText.trim();
        if (labels.includes(text)) {
            const next = td.nextElementSibling;
            if (next) result[text] = next.innerText.trim();
        }
    }
    return result;
}"""

_CONTRACT_FIELDS = [
    "Номер основного договора в реестре договоров",
    "Краткое содержание договора на русском языке",
    "Срок действия договора",
    "Общая итоговая сумма договора",
    "Общая фактическая сумма договора",
]


def parse_contract(page: Page, url: str, bin_number: str) -> ContractRecord:
    for attempt in range(1, RETRY_COUNT + 2):
        try:
            page.goto(url, wait_until="domcontentloaded",
                      timeout=PLAYWRIGHT_CONFIG["timeout"])
            page.wait_for_timeout(150)

            data = page.evaluate(_EXTRACT_JS, _CONTRACT_FIELDS)

            contract_number   = data.get("Номер основного договора в реестре договоров", "")
            description       = data.get("Краткое содержание договора на русском языке", "")
            validity_period   = data.get("Срок действия договора", "")
            amount_str_final  = data.get("Общая итоговая сумма договора", "")
            amount_str_actual = data.get("Общая фактическая сумма договора", "")

            amount_final  = _parse_amount(amount_str_final)
            amount_actual = _parse_amount(amount_str_actual)

            return ContractRecord(
                bin=bin_number,
                contract_number=contract_number,
                description=description or "(описание отсутствует)",
                validity_period=validity_period,
                amount_final=amount_final,
                amount_actual=amount_actual,
                difference=round(amount_final - amount_actual, 2),
                url=url,
                error="" if (amount_str_final and amount_str_actual) else "Сумма не найдена",
            )

        except Exception as exc:
            logger.warning("Попытка %d/%d для %s: %s", attempt, RETRY_COUNT + 1, url, exc)
            if attempt > RETRY_COUNT:
                return ContractRecord(
                    bin=bin_number,
                    contract_number="",
                    description="",
                    validity_period="",
                    amount_final=0.0,
                    amount_actual=0.0,
                    difference=0.0,
                    url=url,
                    error=f"Ошибка загрузки: {exc}",
                )
            time.sleep(2)


# ---------------------------------------------------------------------------
# Парсинг одного БИН (sequential — Playwright sync API не thread-safe)
# ---------------------------------------------------------------------------

def scrape_bin(
    bin_number: str,
    context: BrowserContext,
    progress_cb: ProgressCallback | None = None,
    on_record: "RecordCallback | None" = None,
) -> ScrapeResult:
    result = ScrapeResult(bin=bin_number)

    # Отдельная страница для навигации и сбора ссылок
    nav_page = context.new_page()
    _setup_page_routes(nav_page)
    try:
        navigate_to_registry(nav_page)
        apply_filters(nav_page, bin_number)
        links = collect_contract_links(nav_page)
    finally:
        nav_page.close()  # сразу освобождаем память навигационной страницы

    if not links:
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

    total        = len(links)
    contract_page = context.new_page()
    _setup_page_routes(contract_page)

    try:
        for idx, url in enumerate(links, start=1):
            if progress_cb:
                progress_cb(idx, total, f"Договор {idx} из {total}")

            # Переоткрываем страницу каждые N договоров — очищаем накопленную память
            if idx > 1 and (idx - 1) % PAGE_RECYCLE_INTERVAL == 0:
                contract_page.close()
                contract_page = context.new_page()
                _setup_page_routes(contract_page)
                logger.info("Страница переоткрыта после договора %d", idx - 1)

            try:
                record = parse_contract(contract_page, url, bin_number)
            except Exception as exc:
                logger.error("Необработанная ошибка договора %s: %s", url, exc)
                record = ContractRecord(
                    bin=bin_number,
                    contract_number="",
                    description="",
                    validity_period="",
                    amount_final=0.0,
                    amount_actual=0.0,
                    difference=0.0,
                    url=url,
                    error=f"Критическая ошибка: {exc}",
                )

            result.records.append(record)

            if on_record:
                try:
                    on_record(record)  # немедленно сохраняем на диск
                except Exception as exc:
                    logger.warning("on_record callback error: %s", exc)

            if record.error:
                result.errors.append(f"{url}: {record.error}")

            _random_delay(REQUEST_DELAY)

    except Exception as exc:
        logger.error("Критическая ошибка при обработке БИН %s: %s", bin_number, exc)
        result.errors.append(str(exc))
    finally:
        try:
            contract_page.close()
        except Exception:
            pass

    return result


# ---------------------------------------------------------------------------
# Batch-запуск нескольких БИН (синхронный)
# ---------------------------------------------------------------------------

def scrape_all(
    bin_list: list[str],
    on_bin_start: Callable[[int, int, str], None] | None = None,
    on_contract_progress: ProgressCallback | None = None,
    on_record: "RecordCallback | None" = None,
) -> list[ScrapeResult]:
    results: list[ScrapeResult] = []

    with sync_playwright() as pw:
        browser: Browser = pw.chromium.launch(
            headless=PLAYWRIGHT_CONFIG["headless"],
            args=[
                "--disable-blink-features=AutomationControlled",
                # Безопасность / sandbox (обязательно на Render)
                "--no-sandbox",
                "--disable-setuid-sandbox",
                # Память — самое важное для Render free tier (512MB)
                "--disable-dev-shm-usage",   # не использовать /dev/shm (мало места)
                "--disable-gpu",             # GPU не нужен в headless
                "--no-zygote",               # убирает лишний форк-процесс
                # Отключаем всё лишнее
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--disable-client-side-phishing-detection",
                "--disable-default-apps",
                "--disable-hang-monitor",
                "--disable-sync",
                "--metrics-recording-only",
                "--mute-audio",
                "--no-first-run",
                "--safebrowsing-disable-auto-update",
            ],
        )
        context: BrowserContext = browser.new_context(
            viewport=PLAYWRIGHT_CONFIG["viewport"],
            locale=PLAYWRIGHT_CONFIG["locale"],
            user_agent=PLAYWRIGHT_CONFIG["user_agent"],
            extra_http_headers={
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;"
                    "q=0.9,image/webp,*/*;q=0.8"
                ),
            },
        )
        context.set_default_timeout(PLAYWRIGHT_CONFIG["timeout"])

        try:
            total_bins = len(bin_list)
            for i, bin_number in enumerate(bin_list, start=1):
                if on_bin_start:
                    on_bin_start(i, total_bins, bin_number)

                logger.info("=== Обработка БИН %s (%d/%d) ===", bin_number, i, total_bins)
                result = scrape_bin(bin_number, context, on_contract_progress, on_record)
                results.append(result)

        finally:
            context.close()
            browser.close()

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
