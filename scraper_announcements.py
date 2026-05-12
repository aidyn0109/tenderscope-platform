"""
scraper_announcements.py — Логика парсинга закупочных объявлений с goszakup.gov.kz

Алгоритм:
1. Открыть https://goszakup.gov.kz/ → Закупки → Поиск объявлений
2. Установить фильтры:
     • Статус: «Завершено» и «Формирование протокола итогов»
     • Предмет закупки: «Работа»
     • Сумма закупки с: 1 500 000 000
     • Окончание приема заявок с: дата, выбранная пользователем
3. Нажать «Найти» и собрать все объявления (с пагинацией).
4. Для каждого объявления:
     • открыть страницу;
     • из вкладки «Информация о победителях» извлечь БИН и наименование;
     • если во вкладке «Договоры» нет данных — открыть «Протокол итогов»
       и извлечь «Цена поставщика» по БИН победителя;
     • если данные в «Договоры» есть — оставить цену пустой (0.0).

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

BASE_URL = "https://goszakup.gov.kz"
ANNOUNCEMENTS_URL = f"{BASE_URL}/ru/search/announce"

# Минимальная сумма закупки (тг)
MIN_PURCHASE_SUM = "1500000000"

# Целевые статусы, выбираемые по видимому тексту опции
TARGET_STATUS_TEXTS = ["Завершено", "Формирование протокола итогов"]
# Целевой предмет закупки
TARGET_SUBJECT_TEXT = "Работа"

BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}

REQUEST_DELAY = 0.4
RETRY_COUNT = 2
PAGE_RECYCLE_INTERVAL = 5

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
    winner_price: float          # цена победителя (0 если нет данных или есть договоры)
    url: str                     # гиперссылка на объявление
    error: str = ""              # ошибка при парсинге


@dataclass
class ScrapeAnnouncementsResult:
    selected_date: str
    records: list[AnnouncementRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


ProgressCallback = Callable[[int, int, str], None]
RecordCallback = Callable[["AnnouncementRecord"], None]


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
    time.sleep(base * random.uniform(0.7, 1.3))


def _setup_page_routes(page: Page) -> None:
    page.route(
        "**/*",
        lambda route: (
            route.abort()
            if route.request.resource_type in BLOCKED_RESOURCE_TYPES
            else route.continue_()
        ),
    )


def _extract_bin(text: str) -> str:
    m = re.search(r"\b\d{12}\b", text or "")
    return m.group(0) if m else ""


def _clean_company_name(text: str, bin_value: str) -> str:
    """Удаляет БИН и служебные подстроки, возвращая чистое наименование."""
    if not text:
        return ""
    name = text
    if bin_value:
        name = name.replace(bin_value, "")
    # Убираем переносы / лишние пробелы
    name = re.sub(r"\s+", " ", name)
    # Убираем хвосты вида "БИН:", "ИНН/УНП:"
    name = re.sub(r"\b(БИН|ИНН|УНП|БИН/ИНН|ИНН/УНП)\s*[:№]?\s*", "", name, flags=re.I)
    return name.strip(" ,.;:-")


# ---------------------------------------------------------------------------
# Навигация
# ---------------------------------------------------------------------------

def navigate_to_announcements(page: Page) -> None:
    logger.info("Переходим к поиску объявлений: %s", ANNOUNCEMENTS_URL)
    _setup_page_routes(page)
    for attempt in range(1, 4):
        try:
            page.goto(ANNOUNCEMENTS_URL, wait_until="domcontentloaded",
                      timeout=PLAYWRIGHT_CONFIG["timeout"])
            page.wait_for_timeout(700)
            logger.info("Страница поиска объявлений загружена (попытка %d). URL: %s",
                        attempt, page.url)
            return
        except Exception as exc:
            logger.warning("navigate_to_announcements попытка %d/3: %s", attempt, exc)
            if attempt == 3:
                raise
            time.sleep(5)


# ---------------------------------------------------------------------------
# Установка фильтров
# ---------------------------------------------------------------------------

_SELECT_BY_TEXT_JS = """({selector, targets, exact}) => {
    const sel = document.querySelector(selector);
    if (!sel) return false;
    const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    const wanted = targets.map(norm);
    let changed = 0;
    Array.from(sel.options).forEach(opt => {
        const t = norm(opt.textContent);
        const match = wanted.some(w => exact ? t === w : (t === w || t.includes(w)));
        opt.selected = match;
        if (match) changed += 1;
    });
    if (window.jQuery) {
        jQuery(sel).trigger('change');
    } else {
        sel.dispatchEvent(new Event('change', {bubbles: true}));
    }
    return changed > 0;
}"""


_FILL_INPUT_BY_LABEL_JS = """({labelText, value}) => {
    const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    const target = norm(labelText);

    const findInputNear = (el) => {
        if (!el) return null;
        // 1) label[for=...]
        if (el.htmlFor) {
            const byId = document.getElementById(el.htmlFor);
            if (byId && (byId.tagName === 'INPUT' || byId.tagName === 'SELECT')) return byId;
        }
        // 2) input внутри
        const inner = el.querySelector('input:not([type="hidden"])');
        if (inner) return inner;
        // 3) input в соседних элементах
        let sib = el.nextElementSibling;
        while (sib) {
            if (sib.tagName === 'INPUT') return sib;
            const innerSib = sib.querySelector('input:not([type="hidden"])');
            if (innerSib) return innerSib;
            sib = sib.nextElementSibling;
        }
        // 4) input в родительском контейнере
        const parent = el.closest('.form-group, .form-row, .field, .row, div');
        if (parent) {
            const fromParent = parent.querySelector('input:not([type="hidden"])');
            if (fromParent && fromParent !== el) return fromParent;
        }
        return null;
    };

    const candidates = Array.from(document.querySelectorAll(
        'label, .control-label, .form-label, legend, th, td, span'
    ));
    for (const c of candidates) {
        const text = norm(c.textContent);
        if (!text) continue;
        if (text === target || text.startsWith(target) || text.includes(target)) {
            const input = findInputNear(c);
            if (input) {
                input.focus();
                input.value = value;
                input.dispatchEvent(new Event('input', {bubbles: true}));
                input.dispatchEvent(new Event('change', {bubbles: true}));
                input.blur();
                return true;
            }
        }
    }
    return false;
}"""


def _select_options_by_text(page: Page, selector: str,
                            texts: list[str], exact: bool = False) -> bool:
    return bool(page.evaluate(
        _SELECT_BY_TEXT_JS,
        {"selector": selector, "targets": texts, "exact": exact},
    ))


def _fill_input_by_label(page: Page, label_text: str, value: str,
                         fallback_names: list[str] | None = None) -> bool:
    ok = bool(page.evaluate(_FILL_INPUT_BY_LABEL_JS,
                            {"labelText": label_text, "value": value}))
    if ok:
        return True

    if fallback_names:
        for name in fallback_names:
            loc = page.locator(f"input[name='{name}']")
            if loc.count():
                try:
                    loc.first.fill(value)
                    return True
                except Exception:
                    continue

    logger.warning("Не удалось найти поле по label '%s'", label_text)
    return False


def _find_select_for_label(page: Page, label_text: str) -> str | None:
    """Возвращает CSS-селектор для select-а, соответствующего метке."""
    name = page.evaluate(
        """(labelText) => {
            const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const target = norm(labelText);
            const labels = Array.from(document.querySelectorAll(
                'label, .control-label, .form-label, legend, span'
            ));
            for (const lbl of labels) {
                if (norm(lbl.textContent).includes(target)) {
                    let sel = null;
                    if (lbl.htmlFor) {
                        const el = document.getElementById(lbl.htmlFor);
                        if (el && el.tagName === 'SELECT') sel = el;
                    }
                    if (!sel) sel = lbl.querySelector('select');
                    if (!sel) {
                        let sib = lbl.nextElementSibling;
                        while (sib && !sel) {
                            sel = sib.tagName === 'SELECT' ? sib : sib.querySelector('select');
                            sib = sib.nextElementSibling;
                        }
                    }
                    if (!sel) {
                        const parent = lbl.closest('.form-group, .field, .row, div');
                        if (parent) sel = parent.querySelector('select');
                    }
                    if (sel && sel.name) return sel.name;
                }
            }
            return null;
        }""",
        label_text,
    )
    return f"select[name='{name}']" if name else None


def apply_announcements_filters(page: Page, selected_date: str) -> None:
    """
    Применяет фильтры на странице поиска объявлений.
    selected_date — строка вида "YYYY-MM-DD".
    """
    logger.info("Применяем фильтры для поиска объявлений. Дата: %s", selected_date)

    # Дожидаемся, пока форма поиска проявится
    page.wait_for_selector("form, .filter-form, input[type='submit'], button[type='submit']",
                           state="visible",
                           timeout=PLAYWRIGHT_CONFIG["timeout"])
    page.wait_for_timeout(400)

    # ── 1) Статус ──────────────────────────────────────────────────────────
    status_selectors = [
        "select[name='filter[status][]']",
        "select[name='filter[status]']",
    ]
    label_sel = _find_select_for_label(page, "Статус")
    if label_sel:
        status_selectors.insert(0, label_sel)

    status_set = False
    for sel in status_selectors:
        if page.locator(sel).count():
            status_set = _select_options_by_text(page, sel, TARGET_STATUS_TEXTS)
            if status_set:
                logger.info("Статус выставлен через %s", sel)
                break
    if not status_set:
        logger.warning("Не удалось выставить фильтр Статус")

    page.wait_for_timeout(200)

    # ── 2) Предмет закупки ─────────────────────────────────────────────────
    subject_selectors = [
        "select[name='filter[subject][]']",
        "select[name='filter[subject]']",
        "select[name='filter[item_type][]']",
        "select[name='filter[item_type]']",
    ]
    label_sel = _find_select_for_label(page, "Предмет закупки")
    if label_sel:
        subject_selectors.insert(0, label_sel)

    subject_set = False
    for sel in subject_selectors:
        if page.locator(sel).count():
            subject_set = _select_options_by_text(page, sel, [TARGET_SUBJECT_TEXT], exact=True)
            if subject_set:
                logger.info("Предмет закупки выставлен через %s", sel)
                break
    if not subject_set:
        logger.warning("Не удалось выставить фильтр Предмет закупки")

    page.wait_for_timeout(200)

    # ── 3) Сумма закупки с ─────────────────────────────────────────────────
    _fill_input_by_label(
        page, "Сумма закупки с", MIN_PURCHASE_SUM,
        fallback_names=[
            "filter[sum_min]",
            "filter[amount_from]",
            "filter[amount_min]",
            "filter[count_min]",
            "filter[total_sum_from]",
        ],
    )
    page.wait_for_timeout(150)

    # ── 4) Окончание приема заявок с ───────────────────────────────────────
    _fill_input_by_label(
        page, "Окончание приема заявок с", selected_date,
        fallback_names=[
            "filter[date_acceptance_of_applications_end]",
            "filter[end_date_acceptance_from]",
            "filter[end_date_from]",
            "filter[date_end_from]",
        ],
    )
    page.wait_for_timeout(150)

    # ── 5) Поиск ───────────────────────────────────────────────────────────
    # Берём submit-кнопку именно из формы фильтров (первую видимую)
    search_btn = page.locator(
        "button[type='submit'], input[type='submit']"
    ).first
    search_btn.wait_for(state="visible", timeout=PLAYWRIGHT_CONFIG["timeout"])
    search_btn.click()
    page.wait_for_load_state("domcontentloaded", timeout=PLAYWRIGHT_CONFIG["timeout"])
    page.wait_for_timeout(800)

    logger.info("Фильтры применены. URL: %s", page.url)


# ---------------------------------------------------------------------------
# Сбор объявлений из таблицы (включая пагинацию)
# ---------------------------------------------------------------------------

_COLLECT_ROWS_JS = """() => {
    // Ищем таблицу результатов: содержит ссылку на объявление в колонке "Наименование".
    const link = document.querySelector(
        "a[href*='/announce/index/'], a[href*='/announcement/'], a[href*='/announce/show/']"
    );
    if (!link) return [];
    const table = link.closest('table');
    if (!table) return [];

    // Карта заголовков → индекс
    const headerCells = Array.from(
        (table.tHead && table.tHead.rows[0])
        ? table.tHead.rows[0].cells
        : (table.rows[0] ? table.rows[0].cells : [])
    );
    const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    const headers = headerCells.map(c => norm(c.innerText));

    const indexOfHeader = (substr) => headers.findIndex(h => h.includes(substr));

    const idxNumber = indexOfHeader('№');
    const idxName   = (() => {
        let i = indexOfHeader('наименование');
        if (i === -1) i = indexOfHeader('название');
        return i;
    })();
    const idxMethod = indexOfHeader('способ');
    const idxStart  = indexOfHeader('начало');
    const idxEnd    = indexOfHeader('окончание');
    const idxSum    = (() => {
        let i = indexOfHeader('сумма');
        if (i === -1) i = indexOfHeader('стоимость');
        return i;
    })();
    const idxStatus = indexOfHeader('статус');

    const out = [];
    const bodyRows = table.tBodies[0] ? table.tBodies[0].rows : table.rows;
    for (const row of bodyRows) {
        // Пропускаем строки заголовка
        if (row.cells.length === 0) continue;
        if (row.cells[0] && row.cells[0].tagName === 'TH') continue;

        const anchor = row.querySelector(
            "a[href*='/announce/index/'], a[href*='/announcement/'], a[href*='/announce/show/']"
        );
        if (!anchor) continue;

        const href = anchor.getAttribute('href') || '';
        const cells = row.cells;
        const get = (idx) => (idx >= 0 && idx < cells.length ? cells[idx].innerText.trim() : '');

        const name = (anchor.innerText || '').trim() || get(idxName);

        out.push({
            number:     get(idxNumber),
            name:       name,
            method:     get(idxMethod),
            start_date: get(idxStart),
            end_date:   get(idxEnd),
            sum_amount: get(idxSum),
            status:     get(idxStatus),
            url:        href,
        });
    }
    return out;
}"""


def collect_announcement_links(page: Page) -> list[dict]:
    """
    Собирает информацию обо всех объявлениях (по всем страницам пагинации).
    """
    announcements: list[dict] = []
    seen_urls: set[str] = set()
    page_num = 1

    while True:
        logger.info("Сбор объявлений — страница %d", page_num)

        try:
            page.wait_for_selector("table", state="attached",
                                   timeout=PLAYWRIGHT_CONFIG["timeout"])
        except Exception:
            logger.warning("Таблица результатов не появилась на странице %d", page_num)
            break

        rows = page.evaluate(_COLLECT_ROWS_JS)
        new_count = 0
        for row in rows:
            url = row.get("url", "")
            if not url:
                continue
            absolute = url if url.startswith("http") else BASE_URL + url
            if absolute in seen_urls:
                continue
            seen_urls.add(absolute)
            row["url"] = absolute
            announcements.append(row)
            new_count += 1

        logger.info("Страница %d: %d новых объявлений (всего: %d)",
                    page_num, new_count, len(announcements))

        if new_count == 0:
            break

        # Пагинация: ищем ссылку «»»
        next_li = page.locator(".pagination li").filter(has_text="»").first
        if not next_li.count():
            break
        try:
            is_disabled = next_li.evaluate(
                "el => el.classList.contains('disabled') || el.classList.contains('active')"
            )
        except Exception:
            is_disabled = False
        if is_disabled:
            break

        try:
            next_li.locator("a").first.click()
            page.wait_for_load_state("domcontentloaded",
                                     timeout=PLAYWRIGHT_CONFIG["timeout"])
            page.wait_for_timeout(500)
        except Exception as exc:
            logger.warning("Не удалось перейти на следующую страницу: %s", exc)
            break

        page_num += 1
        if page_num > 100:
            logger.warning("Достигнут лимит страниц пагинации (100)")
            break

        _random_delay(0.3)

    logger.info("Всего объявлений: %d", len(announcements))
    return announcements


# ---------------------------------------------------------------------------
# Парсинг одного объявления
# ---------------------------------------------------------------------------

def _click_tab(page: Page, tab_text: str) -> bool:
    """
    Кликает по вкладке (li / a / button) с заданным видимым текстом.
    Возвращает True, если клик прошёл успешно.
    """
    candidates = [
        page.locator("ul.nav li a").filter(has_text=tab_text),
        page.locator("a[role='tab']").filter(has_text=tab_text),
        page.locator("a[data-toggle='tab']").filter(has_text=tab_text),
        page.locator("a").filter(has_text=tab_text),
        page.locator("button").filter(has_text=tab_text),
    ]
    for loc in candidates:
        if loc.count():
            try:
                loc.first.click()
                page.wait_for_timeout(450)
                return True
            except Exception:
                continue
    return False


_HAS_CONTRACTS_JS = """() => {
    // Найти активную панель вкладки
    const panes = document.querySelectorAll('.tab-pane.active, [role="tabpanel"]');
    let pane = null;
    for (const p of panes) {
        if (p.offsetParent !== null) { pane = p; break; }
    }
    if (!pane) return false;

    // Считаем содержательные строки в любой таблице внутри панели.
    const tables = pane.querySelectorAll('table');
    for (const t of tables) {
        const rows = t.querySelectorAll('tbody tr');
        let real = 0;
        for (const r of rows) {
            const txt = (r.innerText || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            if (!txt) continue;
            if (txt.includes('нет данных') || txt.includes('отсутству') ||
                txt.includes('не найдено')) continue;
            real += 1;
        }
        if (real > 0) return true;
    }
    // На случай, если данные показаны не таблицей
    const txt = (pane.innerText || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    if (txt.length > 80 && !txt.includes('нет данных') && !txt.includes('отсутству')) {
        // Эвристика: должна присутствовать характерная подпись договора
        if (txt.includes('договор') || txt.includes('номер договора')) return true;
    }
    return false;
}"""


_PARSE_WINNER_JS = """() => {
    const panes = document.querySelectorAll('.tab-pane.active, [role="tabpanel"]');
    let pane = null;
    for (const p of panes) {
        if (p.offsetParent !== null) { pane = p; break; }
    }
    if (!pane) return null;

    const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    const tables = pane.querySelectorAll('table');
    for (const table of tables) {
        // Заголовок
        let headerCells = [];
        if (table.tHead && table.tHead.rows[0]) {
            headerCells = Array.from(table.tHead.rows[0].cells);
        } else if (table.rows[0]) {
            headerCells = Array.from(table.rows[0].cells);
        }
        const headers = headerCells.map(c => norm(c.innerText));
        const winnerIdx = headers.findIndex(h => h.includes('победител'));
        if (winnerIdx === -1) continue;

        const bodyRows = table.tBodies[0] ? table.tBodies[0].rows : Array.from(table.rows).slice(1);
        for (const r of bodyRows) {
            const cells = r.cells;
            if (cells.length <= winnerIdx) continue;
            const cellText = (cells[winnerIdx].innerText || '').trim();
            if (cellText) return cellText;
        }
    }
    return null;
}"""


_PARSE_PROTOCOL_PRICE_JS = """(winnerBin) => {
    const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    const tables = document.querySelectorAll('table');
    for (const table of tables) {
        const tableTxt = norm(table.innerText);
        if (!tableTxt.includes('цена') && !tableTxt.includes('расчет условных цен')) continue;

        let headerCells = [];
        if (table.tHead && table.tHead.rows[0]) {
            headerCells = Array.from(table.tHead.rows[0].cells);
        } else if (table.rows[0]) {
            headerCells = Array.from(table.rows[0].cells);
        }
        const headers = headerCells.map(c => norm(c.innerText));

        let binCol = headers.findIndex(h =>
            h.includes('бин') || h.includes('инн') || h.includes('унп'));
        let priceCol = headers.findIndex(h =>
            h.includes('цена') && (h.includes('поставщик') || h.includes('участник')));
        if (priceCol === -1) priceCol = headers.findIndex(h => h.includes('цена поставщика'));
        if (priceCol === -1) priceCol = headers.findIndex(h => h === 'цена');

        if (binCol === -1 || priceCol === -1) continue;

        const bodyRows = table.tBodies[0] ? table.tBodies[0].rows : Array.from(table.rows).slice(1);
        for (const r of bodyRows) {
            const cells = r.cells;
            if (cells.length <= Math.max(binCol, priceCol)) continue;
            const binText = (cells[binCol].innerText || '').trim();
            if (binText.includes(winnerBin)) {
                return (cells[priceCol].innerText || '').trim();
            }
        }
    }
    return null;
}"""


def _has_contracts_data(page: Page) -> bool:
    """Возвращает True, если во вкладке «Договоры» есть содержательные записи."""
    if not _click_tab(page, "Договоры"):
        return False
    try:
        return bool(page.evaluate(_HAS_CONTRACTS_JS))
    except Exception as exc:
        logger.debug("_has_contracts_data error: %s", exc)
        return False


def _parse_winner(page: Page) -> tuple[str, str]:
    """Возвращает (наименование_компании, БИН) из вкладки «Информация о победителях»."""
    if not _click_tab(page, "Информация о победителях") and not _click_tab(page, "победителях"):
        return "", ""
    try:
        raw = page.evaluate(_PARSE_WINNER_JS)
    except Exception as exc:
        logger.debug("_parse_winner error: %s", exc)
        return "", ""

    if not raw:
        return "", ""
    winner_bin = _extract_bin(raw)
    winner_name = _clean_company_name(raw, winner_bin)
    return winner_name, winner_bin


def _parse_protocol_price(page: Page, winner_bin: str) -> float:
    """Открывает «Просмотреть протокол» во вкладке «Протоколы» и достаёт цену поставщика."""
    if not winner_bin:
        return 0.0
    if not _click_tab(page, "Протоколы"):
        return 0.0

    view_btn_candidates = [
        page.locator("a:has-text('Просмотреть протокол')"),
        page.locator("button:has-text('Просмотреть протокол')"),
        page.locator("a:has-text('Просмотреть')"),
        page.locator("button:has-text('Просмотреть')"),
    ]
    view_btn = None
    for loc in view_btn_candidates:
        if loc.count():
            view_btn = loc.first
            break
    if view_btn is None:
        logger.debug("Кнопка «Просмотреть протокол» не найдена")
        return 0.0

    protocol_page = None
    opened_new = False
    try:
        try:
            with page.context.expect_page(timeout=4000) as new_info:
                view_btn.click()
            protocol_page = new_info.value
            protocol_page.wait_for_load_state("domcontentloaded",
                                              timeout=PLAYWRIGHT_CONFIG["timeout"])
            opened_new = True
        except Exception:
            view_btn.click()
            page.wait_for_load_state("domcontentloaded",
                                     timeout=PLAYWRIGHT_CONFIG["timeout"])
            protocol_page = page

        protocol_page.wait_for_timeout(600)

        raw_price = protocol_page.evaluate(_PARSE_PROTOCOL_PRICE_JS, winner_bin)
        return _parse_amount(raw_price) if raw_price else 0.0

    except Exception as exc:
        logger.debug("_parse_protocol_price error: %s", exc)
        return 0.0
    finally:
        if opened_new and protocol_page is not None and protocol_page is not page:
            try:
                protocol_page.close()
            except Exception:
                pass


def parse_announcement(page: Page, ann_data: dict, index: int) -> AnnouncementRecord:
    """
    Парсит одно объявление по правилам алгоритма.

    Алгоритм:
      1. Открыть страницу объявления.
      2. Вкладка «Договоры»: если есть данные → парсим только победителя
         (цена остаётся пустой). Если нет → парсим победителя и цену из протокола.
    """
    url = ann_data["url"]
    number_str = (ann_data.get("number") or "").strip()
    number = int(number_str) if number_str.isdigit() else index

    record = AnnouncementRecord(
        number=number,
        name=ann_data.get("name", ""),
        method=ann_data.get("method", ""),
        start_date=ann_data.get("start_date", ""),
        end_date=ann_data.get("end_date", ""),
        sum_amount=_parse_amount(ann_data.get("sum_amount", "")),
        status=ann_data.get("status", ""),
        winner_name="",
        winner_bin="",
        winner_price=0.0,
        url=url,
        error="",
    )

    for attempt in range(1, RETRY_COUNT + 2):
        try:
            page.goto(url, wait_until="domcontentloaded",
                      timeout=PLAYWRIGHT_CONFIG["timeout"])
            page.wait_for_timeout(400)
            break
        except Exception as exc:
            if attempt > RETRY_COUNT:
                record.error = f"Ошибка загрузки: {exc}"
                return record
            time.sleep(2)

    has_contracts = _has_contracts_data(page)
    logger.debug("URL=%s has_contracts=%s", url, has_contracts)

    winner_name, winner_bin = _parse_winner(page)
    record.winner_name = winner_name
    record.winner_bin = winner_bin

    if has_contracts:
        # По заданию: оставляем цену пустой, всё остальное заполняем.
        record.winner_price = 0.0
    else:
        record.winner_price = _parse_protocol_price(page, winner_bin)

    if not winner_bin:
        record.error = "Победитель не найден"

    return record


# ---------------------------------------------------------------------------
# Основная функция парсинга
# ---------------------------------------------------------------------------

def scrape_announcements(
    selected_date: str,
    on_progress: ProgressCallback | None = None,
    on_record: RecordCallback | None = None,
) -> ScrapeAnnouncementsResult:
    """
    Главная функция парсинга объявлений.
    selected_date — строка вида "YYYY-MM-DD".
    """
    result = ScrapeAnnouncementsResult(selected_date=selected_date)

    nav_page = None
    announcement_page = None

    try:
        with sync_playwright() as pw:
            browser: Browser = pw.chromium.launch(
                headless=PLAYWRIGHT_CONFIG["headless"],
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--no-zygote",
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
                # Поиск и сбор ссылок
                nav_page = context.new_page()
                _setup_page_routes(nav_page)

                if on_progress:
                    on_progress(0, 0, "Открываем поиск объявлений...")

                navigate_to_announcements(nav_page)

                if on_progress:
                    on_progress(0, 0, "Применяем фильтры...")

                apply_announcements_filters(nav_page, selected_date)

                if on_progress:
                    on_progress(0, 0, "Собираем список объявлений...")

                ann_list = collect_announcement_links(nav_page)

                nav_page.close()
                nav_page = None

                if not ann_list:
                    logger.warning("Объявления не найдены для даты: %s", selected_date)
                    if on_progress:
                        on_progress(0, 0, "Объявления не найдены")
                    return result

                total = len(ann_list)
                announcement_page = context.new_page()
                _setup_page_routes(announcement_page)

                for idx, ann_data in enumerate(ann_list, start=1):
                    if on_progress:
                        on_progress(idx, total, f"Объявление {idx} из {total}")

                    if idx > 1 and (idx - 1) % PAGE_RECYCLE_INTERVAL == 0:
                        try:
                            announcement_page.close()
                        except Exception:
                            pass
                        announcement_page = context.new_page()
                        _setup_page_routes(announcement_page)
                        logger.info("Страница переоткрыта после объявления %d", idx - 1)

                    try:
                        record = parse_announcement(announcement_page, ann_data, idx)
                    except Exception as exc:
                        logger.error("Необработанная ошибка объявления %s: %s",
                                     ann_data.get("url", "?"), exc)
                        record = AnnouncementRecord(
                            number=idx,
                            name=ann_data.get("name", ""),
                            method=ann_data.get("method", ""),
                            start_date=ann_data.get("start_date", ""),
                            end_date=ann_data.get("end_date", ""),
                            sum_amount=_parse_amount(ann_data.get("sum_amount", "")),
                            status=ann_data.get("status", ""),
                            winner_name="",
                            winner_bin="",
                            winner_price=0.0,
                            url=ann_data.get("url", ""),
                            error=f"Критическая ошибка: {exc}",
                        )

                    result.records.append(record)

                    if on_record:
                        try:
                            on_record(record)
                        except Exception as exc:
                            logger.warning("on_record callback error: %s", exc)

                    if record.error:
                        result.errors.append(f"{record.url}: {record.error}")

                    _random_delay(REQUEST_DELAY)

            except Exception as exc:
                logger.error("Критическая ошибка при парсинге объявлений: %s", exc)
                result.errors.append(str(exc))
            finally:
                for p in (nav_page, announcement_page):
                    if p is not None:
                        try:
                            p.close()
                        except Exception:
                            pass
                context.close()
                browser.close()

    except Exception as exc:
        logger.error("Ошибка инициализации браузера: %s", exc)
        result.errors.append(f"Ошибка инициализации браузера: {exc}")

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
