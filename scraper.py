"""
scraper.py — Логика парсинга goszakup.gov.kz
Использует синхронный Playwright API (sync_playwright).
"""

import logging
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
    "timeout": 60_000,
    "viewport": {"width": 1280, "height": 800},
    "locale": "ru-RU",
}

BASE_URL     = "https://goszakup.gov.kz"
REGISTRY_URL = f"{BASE_URL}/ru/registry/contract"

# Реальные значения статусов из select на сайте:
# 190 = Действует, 460 = Передан.Действует, 450 = Создано доп.соглашение
TARGET_STATUS_VALUES = ["190", "460", "450"]

REQUEST_DELAY = 2.5
RETRY_COUNT   = 2

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Типы данных
# ---------------------------------------------------------------------------

@dataclass
class ContractRecord:
    bin:                str
    description:        str
    amount_procurement: float
    amount_final:       float
    difference:         float
    url:                str
    error:              str = ""


@dataclass
class ScrapeResult:
    bin:     str
    records: list[ContractRecord] = field(default_factory=list)
    errors:  list[str]            = field(default_factory=list)


ProgressCallback = Callable[[int, int, str], None]


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _parse_amount(raw: str) -> float:
    if not raw:
        return 0.0
    cleaned = re.sub(r"[^\d,\.]", "", raw.replace("\xa0", "").replace(" ", ""))
    cleaned = cleaned.replace(",", ".")
    # Убираем лишние точки (1.234.567 → 1234567)
    parts = cleaned.split(".")
    if len(parts) > 2:
        cleaned = "".join(parts[:-1]) + "." + parts[-1]
    try:
        return float(cleaned)
    except ValueError:
        logger.warning("Не удалось распарсить сумму: %r", raw)
        return 0.0


# ---------------------------------------------------------------------------
# Навигация к реестру договоров
# ---------------------------------------------------------------------------

def navigate_to_registry(page: Page) -> None:
    logger.info("Переходим на реестр договоров: %s", REGISTRY_URL)
    page.goto(REGISTRY_URL, wait_until="domcontentloaded",
              timeout=PLAYWRIGHT_CONFIG["timeout"])
    page.wait_for_timeout(1_500)
    logger.info("Реестр загружен. URL: %s", page.url)


# ---------------------------------------------------------------------------
# Применение фильтров
# ---------------------------------------------------------------------------

def apply_filters(page: Page, bin_number: str) -> None:
    logger.info("Применяем фильтры для БИН: %s", bin_number)

    # Поле поставщика: id="in_supplier"
    supplier_input = page.locator("#in_supplier")
    supplier_input.wait_for(state="visible", timeout=PLAYWRIGHT_CONFIG["timeout"])
    supplier_input.fill(bin_number)
    page.wait_for_timeout(300)

    # Статусы через Select2 / jQuery (select name="filter[status][]")
    page.evaluate(
        """(values) => {
            const sel = document.querySelector("select[name='filter[status][]']");
            if (!sel) return;
            if (window.jQuery) {
                jQuery(sel).val(values).trigger('change');
            } else {
                // Fallback: выбираем нужные options напрямую
                Array.from(sel.options).forEach(opt => {
                    opt.selected = values.includes(opt.value);
                });
            }
        }""",
        TARGET_STATUS_VALUES,
    )
    page.wait_for_timeout(500)

    # Кнопка «Найти» (submit)
    search_btn = page.locator("button[type='submit']").first
    search_btn.wait_for(state="visible", timeout=PLAYWRIGHT_CONFIG["timeout"])
    search_btn.click()
    page.wait_for_load_state("domcontentloaded", timeout=PLAYWRIGHT_CONFIG["timeout"])
    page.wait_for_timeout(1_000)

    logger.info("Фильтры применены. URL: %s", page.url)


# ---------------------------------------------------------------------------
# Сбор ссылок на договоры (с пагинацией)
# ---------------------------------------------------------------------------

def collect_contract_links(page: Page) -> list[str]:
    """
    Собирает ссылки вида /egzcontract/cpublic/show/{id} со всех страниц.
    Пагинация: кнопка '»' в .pagination (Bootstrap), проверяем disabled-класс.
    """
    links: list[str] = []
    page_num = 1

    while True:
        logger.info("Сбор ссылок — страница %d", page_num)

        anchors = page.locator("a[href*='/egzcontract/cpublic/show/']")
        count   = anchors.count()
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
            break  # Нет новых ссылок — выходим

        # Проверяем кнопку «следующая страница»
        next_li = page.locator(".pagination li").filter(has_text="»").first
        if not next_li.count():
            break

        is_disabled = next_li.evaluate(
            "el => el.classList.contains('disabled') || el.classList.contains('active')"
        )
        if is_disabled:
            break

        # Запоминаем первую ссылку текущей страницы
        first_href_before = links[-(new_count)] if new_count else None

        next_li.locator("a").first.click()
        page.wait_for_load_state("domcontentloaded", timeout=PLAYWRIGHT_CONFIG["timeout"])
        page.wait_for_timeout(1_000)

        # Проверяем, что страница реально сменилась
        first_anchor = page.locator("a[href*='/egzcontract/cpublic/show/']").first
        if first_anchor.count():
            first_href_after = first_anchor.get_attribute("href")
            if first_href_after and (
                first_href_after == first_href_before
                or first_href_after in links
            ):
                break  # Страница не изменилась
        else:
            break

        page_num += 1
        if page_num > 100:  # Защита от бесконечного цикла
            break

        time.sleep(REQUEST_DELAY)

    logger.info("Всего ссылок: %d", len(links))
    return links


# ---------------------------------------------------------------------------
# Парсинг одного договора
# ---------------------------------------------------------------------------

def _extract_field_value(page: Page, label: str) -> str:
    """
    Извлекает значение поля с сайта.
    Структура страницы: <td width="40%">Метка</td><td>Значение</td>
    """
    # Стратегия 1: td с точным текстом метки → следующий td (основная структура сайта)
    value = page.evaluate(
        """(label) => {
            const tds = document.querySelectorAll("td");
            for (const td of tds) {
                if (td.innerText.trim() === label) {
                    const next = td.nextElementSibling;
                    if (next) return next.innerText.trim();
                }
            }
            return "";
        }""",
        label,
    )
    if value:
        return value

    # Стратегия 2: dt → dd
    dd = page.locator(f"dt:has-text('{label}') + dd").first
    if dd.count():
        return dd.inner_text().strip()

    # Стратегия 3: th → td
    row_td = page.locator(
        f"th:has-text('{label}') + td, td:has-text('{label}') + td"
    ).first
    if row_td.count():
        return row_td.inner_text().strip()

    # Стратегия 4: label → связанный элемент
    label_el = page.locator(f"label:has-text('{label}')").first
    if label_el.count():
        for_attr = label_el.get_attribute("for")
        if for_attr:
            el = page.locator(f"#{for_attr}").first
            if el.count():
                tag = el.evaluate("el => el.tagName.toLowerCase()")
                return el.input_value().strip() if tag == "input" else el.inner_text().strip()

    logger.debug("Поле «%s» не найдено", label)
    return ""


def parse_contract(page: Page, url: str, bin_number: str) -> ContractRecord:
    for attempt in range(1, RETRY_COUNT + 2):
        try:
            page.goto(url, wait_until="domcontentloaded",
                      timeout=PLAYWRIGHT_CONFIG["timeout"])
            page.wait_for_timeout(800)

            description      = _extract_field_value(
                page, "Краткое содержание договора на русском языке"
            )
            amount_str_proc  = _extract_field_value(
                page, "Общая сумма договора по итогам закупки"
            )
            amount_str_final = _extract_field_value(
                page, "Общая итоговая сумма договора"
            )

            amount_proc  = _parse_amount(amount_str_proc)
            amount_final = _parse_amount(amount_str_final)

            return ContractRecord(
                bin=bin_number,
                description=description or "(описание отсутствует)",
                amount_procurement=amount_proc,
                amount_final=amount_final,
                difference=round(amount_proc - amount_final, 2),
                url=url,
                error="" if (amount_str_proc and amount_str_final) else "Сумма не найдена",
            )

        except Exception as exc:
            logger.warning("Попытка %d/%d для %s: %s", attempt, RETRY_COUNT + 1, url, exc)
            if attempt > RETRY_COUNT:
                return ContractRecord(
                    bin=bin_number,
                    description="",
                    amount_procurement=0.0,
                    amount_final=0.0,
                    difference=0.0,
                    url=url,
                    error=f"Ошибка загрузки: {exc}",
                )
            time.sleep(2)


# ---------------------------------------------------------------------------
# Парсинг одного БИН
# ---------------------------------------------------------------------------

def scrape_bin(
    bin_number: str,
    context: BrowserContext,
    progress_cb: ProgressCallback | None = None,
) -> ScrapeResult:
    result = ScrapeResult(bin=bin_number)
    page   = context.new_page()

    try:
        navigate_to_registry(page)
        apply_filters(page, bin_number)
        links = collect_contract_links(page)

        if not links:
            result.records.append(ContractRecord(
                bin=bin_number,
                description="Договоры не найдены",
                amount_procurement=0.0,
                amount_final=0.0,
                difference=0.0,
                url="",
                error="Договоры не найдены",
            ))
            return result

        total = len(links)
        for idx, url in enumerate(links, start=1):
            if progress_cb:
                progress_cb(idx, total, f"Договор {idx} из {total}")

            try:
                record = parse_contract(page, url, bin_number)
            except Exception as exc:
                logger.error("Необработанная ошибка договора %s: %s", url, exc)
                record = ContractRecord(
                    bin=bin_number,
                    description="",
                    amount_procurement=0.0,
                    amount_final=0.0,
                    difference=0.0,
                    url=url,
                    error=f"Критическая ошибка: {exc}",
                )

            result.records.append(record)

            if record.error:
                result.errors.append(f"{url}: {record.error}")

            time.sleep(REQUEST_DELAY)

    except Exception as exc:
        logger.error("Критическая ошибка при обработке БИН %s: %s", bin_number, exc)
        result.errors.append(str(exc))
    finally:
        page.close()

    return result


# ---------------------------------------------------------------------------
# Batch-запуск нескольких БИН (синхронный)
# ---------------------------------------------------------------------------

def scrape_all(
    bin_list: list[str],
    on_bin_start: Callable[[int, int, str], None] | None = None,
    on_contract_progress: ProgressCallback | None = None,
) -> list[ScrapeResult]:
    results: list[ScrapeResult] = []

    with sync_playwright() as pw:
        browser: Browser = pw.chromium.launch(
            headless=PLAYWRIGHT_CONFIG["headless"],
        )
        context: BrowserContext = browser.new_context(
            viewport=PLAYWRIGHT_CONFIG["viewport"],
            locale=PLAYWRIGHT_CONFIG["locale"],
        )
        context.set_default_timeout(PLAYWRIGHT_CONFIG["timeout"])

        try:
            total_bins = len(bin_list)
            for i, bin_number in enumerate(bin_list, start=1):
                if on_bin_start:
                    on_bin_start(i, total_bins, bin_number)

                logger.info("=== Обработка БИН %s (%d/%d) ===", bin_number, i, total_bins)
                result = scrape_bin(bin_number, context, on_contract_progress)
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
            print(f"  {rec.description[:60]:60s} | разница: {rec.difference:,.2f} ₸")
        if res.errors:
            print(f"  Ошибок: {len(res.errors)}")
