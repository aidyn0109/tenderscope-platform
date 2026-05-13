"""
excel_export_announcements.py — Формирование Excel-отчёта по результатам парсинга объявлений
"""

import io
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import (
    Alignment,
    Font,
    PatternFill,
    Border,
    Side,
)
from openpyxl.utils import get_column_letter

from scraper_announcements import ScrapeAnnouncementsResult, AnnouncementRecord

# ── Стили ──────────────────────────────────────────────────────────────────

HEADER_FONT   = Font(name="Arial", bold=True, size=10, color="000000")
CELL_FONT     = Font(name="Arial", size=10)
LINK_FONT     = Font(name="Arial", size=10, color="0563C1", underline="single")
SUMMARY_FONT  = Font(name="Arial", bold=True, size=10)
HEADER_FILL   = PatternFill(fill_type="solid", fgColor="D9D9D9")

THIN_SIDE   = Side(style="thin", color="BFBFBF")
THIN_BORDER = Border(left=THIN_SIDE, right=THIN_SIDE, top=THIN_SIDE, bottom=THIN_SIDE)

NUMBER_FORMAT = "#,##0.00"

# Столбцы листа "Объявления"
COL_HEADERS = [
    "№",
    "Наименование объявления",
    "Способ",
    "Начало приема заявок",
    "Окончание приема заявок",
    "Сумма, тг.",
    "Статус",
    "Наименование победителя конкурса",
    "БИН победителя",
    "Цена победителя, тг.",
    "Гиперссылка на объявление",
]
COL_WIDTHS = [6, 50, 20, 22, 22, 20, 22, 35, 16, 20, 50]

# Индексы числовых столбцов (1-based): F=6, J=10
NUMERIC_COLS = {6, 10}
# Индекс столбца ссылок
LINK_COL = 11


def _apply_header_style(cell, text: str) -> None:
    cell.value = text
    cell.font = HEADER_FONT
    cell.fill = HEADER_FILL
    cell.border = THIN_BORDER
    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _apply_cell_style(cell, value, *, is_number: bool = False, is_link: bool = False) -> None:
    cell.value = value
    cell.border = THIN_BORDER

    if is_link:
        cell.font = LINK_FONT
        cell.alignment = Alignment(vertical="top", wrap_text=False)
    elif is_number:
        cell.font = CELL_FONT
        cell.number_format = NUMBER_FORMAT
        cell.alignment = Alignment(horizontal="right", vertical="top")
    else:
        cell.font = CELL_FONT
        cell.alignment = Alignment(vertical="top", wrap_text=True)


def _write_announcements_sheet(wb: Workbook, result: ScrapeAnnouncementsResult) -> None:
    """Лист «Объявления» со всеми записями об объявлениях."""
    ws = wb.create_sheet(title="Объявления")

    # Заголовки
    ws.row_dimensions[1].height = 32
    for col_idx, (header, width) in enumerate(zip(COL_HEADERS, COL_WIDTHS), start=1):
        cell = ws.cell(row=1, column=col_idx)
        _apply_header_style(cell, header)
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    row_idx = 2
    for record in result.records:
        ws.row_dimensions[row_idx].height = 15

        # A — №
        _apply_cell_style(ws.cell(row=row_idx, column=1), record.number)

        # B — Наименование объявления (всегда из листинга; если совсем нет — показываем ошибку)
        if record.name:
            _apply_cell_style(ws.cell(row=row_idx, column=2), record.name)
        elif record.error:
            _apply_cell_style(ws.cell(row=row_idx, column=2), f"⚠ {record.error}")
        else:
            _apply_cell_style(ws.cell(row=row_idx, column=2), "")

        # C — Способ
        _apply_cell_style(ws.cell(row=row_idx, column=3), record.method or "")

        # D — Начало приема заявок
        _apply_cell_style(ws.cell(row=row_idx, column=4), record.start_date or "")

        # E — Окончание приема заявок
        _apply_cell_style(ws.cell(row=row_idx, column=5), record.end_date or "")

        # F — Сумма, тг. (из листинга — всегда заполняем, если есть значение)
        cell_f = ws.cell(row=row_idx, column=6)
        if record.sum_amount > 0:
            _apply_cell_style(cell_f, record.sum_amount, is_number=True)
        else:
            _apply_cell_style(cell_f, "—")

        # G — Статус
        _apply_cell_style(ws.cell(row=row_idx, column=7), record.status or "")

        # H — Наименование победителя
        _apply_cell_style(ws.cell(row=row_idx, column=8), record.winner_name or "—")

        # I — БИН победителя
        _apply_cell_style(ws.cell(row=row_idx, column=9), record.winner_bin or "—")

        # J — Цена победителя, тг.
        #   • если есть данные в «Договоры»  → ячейка пустая (по ТЗ);
        #   • иначе если есть цена           → число;
        #   • иначе                          → «—».
        cell_j = ws.cell(row=row_idx, column=10)
        if record.has_contracts:
            _apply_cell_style(cell_j, "")
        elif record.winner_price > 0:
            _apply_cell_style(cell_j, record.winner_price, is_number=True)
        else:
            _apply_cell_style(cell_j, "—")

        # K — Гиперссылка (всегда показываем, если URL известен)
        cell_k = ws.cell(row=row_idx, column=11)
        if record.url:
            cell_k.value = record.url
            cell_k.hyperlink = record.url
            cell_k.font = LINK_FONT
            cell_k.border = THIN_BORDER
            cell_k.alignment = Alignment(vertical="top", wrap_text=False)
        else:
            _apply_cell_style(cell_k, "—")

        row_idx += 1

    # Итоговая строка
    ws.row_dimensions[row_idx].height = 20

    total_sum = sum(r.sum_amount for r in result.records if not r.error)
    total_price = sum(r.winner_price for r in result.records if not r.error and r.winner_price > 0)

    for col_idx in range(1, len(COL_HEADERS) + 1):
        cell = ws.cell(row=row_idx, column=col_idx)
        cell.fill = PatternFill(fill_type="solid", fgColor="F2F2F2")
        cell.border = THIN_BORDER
        cell.font = SUMMARY_FONT

    lbl = ws.cell(row=row_idx, column=2)
    lbl.value = "ИТОГО:"
    lbl.alignment = Alignment(horizontal="right", vertical="center")

    # Сумма
    cell_sum = ws.cell(row=row_idx, column=6)
    cell_sum.value = total_sum
    cell_sum.number_format = NUMBER_FORMAT
    cell_sum.alignment = Alignment(horizontal="right", vertical="center")

    # Цена
    cell_price = ws.cell(row=row_idx, column=10)
    cell_price.value = total_price
    cell_price.number_format = NUMBER_FORMAT
    cell_price.alignment = Alignment(horizontal="right", vertical="center")

    ws.freeze_panes = "A2"


def build_excel_announcements_report(result: ScrapeAnnouncementsResult) -> bytes:
    """
    Формирует Excel-книгу по результатам парсинга объявлений.

    Структура:
      - Лист «Объявления» — таблица всех объявлений с информацией о победителях

    Возвращает байты .xlsx файла для st.download_button.
    """
    wb = Workbook()
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    _write_announcements_sheet(wb, result)

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def get_announcements_report_filename() -> str:
    return f"goszakup_announcements_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
