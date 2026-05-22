"""
excel_export_announcements.py — Формирование Excel-отчёта по результатам парсинга объявлений.
Каждый лот объявления выводится отдельной строкой.
"""

import io
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
from openpyxl.utils import get_column_letter

from scraper_announcements import ScrapeAnnouncementsResult, AnnouncementRecord

# ── Стили ──────────────────────────────────────────────────────────────────

HEADER_FONT  = Font(name="Arial", bold=True, size=10, color="000000")
CELL_FONT    = Font(name="Arial", size=10)
LINK_FONT    = Font(name="Arial", size=10, color="0563C1", underline="single")
SUMMARY_FONT = Font(name="Arial", bold=True, size=10)
HEADER_FILL  = PatternFill(fill_type="solid", fgColor="D9D9D9")
LOT_FILL     = PatternFill(fill_type="solid", fgColor="EBF3FB")  # голубоватый для строк лотов

THIN_SIDE   = Side(style="thin", color="BFBFBF")
THIN_BORDER = Border(left=THIN_SIDE, right=THIN_SIDE, top=THIN_SIDE, bottom=THIN_SIDE)

NUMBER_FORMAT = "#,##0.00"

# Столбцы листа "Объявления"
COL_HEADERS = [
    "№",                                    # A
    "Наименование объявления",              # B
    "Способ",                               # C
    "Начало приема заявок",                 # D
    "Окончание приема заявок",              # E
    "Сумма закупки, тг.",                   # F
    "Статус",                               # G
    "№ лота",                               # H
    "Наименование лота",                    # I
    "Наименование победителя конкурса",     # J
    "БИН победителя",                       # K
    "Цена победителя, тг.",                 # L
    "Гиперссылка на объявление",            # M
]
COL_WIDTHS = [6, 45, 20, 22, 22, 20, 22, 18, 40, 35, 16, 20, 50]


def _apply_header_style(cell, text: str) -> None:
    cell.value = text
    cell.font = HEADER_FONT
    cell.fill = HEADER_FILL
    cell.border = THIN_BORDER
    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _apply_cell_style(cell, value, *, is_number: bool = False,
                      is_link: bool = False, fill=None) -> None:
    cell.value = value
    cell.border = THIN_BORDER
    if fill:
        cell.fill = fill

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
    ws = wb.create_sheet(title="Объявления")

    # Заголовки
    ws.row_dimensions[1].height = 32
    for col_idx, (header, width) in enumerate(zip(COL_HEADERS, COL_WIDTHS), start=1):
        cell = ws.cell(row=1, column=col_idx)
        _apply_header_style(cell, header)
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    row_idx = 2
    total_price = 0.0
    total_sum = 0.0

    for record in result.records:
        lots = record.lots

        # Если лотов нет — выводим одну строку без данных о лоте
        if not lots:
            ws.row_dimensions[row_idx].height = 15
            _write_announcement_row(ws, row_idx, record,
                                    lot_number="", lot_name="",
                                    winner_name=record.winner_name,
                                    winner_bin=record.winner_bin,
                                    winner_price=record.winner_price,
                                    is_first_lot=True)
            if record.winner_price > 0:
                total_price += record.winner_price
            if record.sum_amount > 0:
                total_sum += record.sum_amount
            row_idx += 1
            continue

        # Выводим строку для каждого лота
        for lot_idx, lot in enumerate(lots):
            ws.row_dimensions[row_idx].height = 15
            is_first = (lot_idx == 0)
            _write_announcement_row(
                ws, row_idx, record,
                lot_number=lot.lot_number,
                lot_name=lot.lot_name,
                winner_name=lot.winner_name,
                winner_bin=lot.winner_bin,
                winner_price=lot.winner_price,
                is_first_lot=is_first,
                fill=None if is_first else LOT_FILL,
            )
            if lot.winner_price > 0:
                total_price += lot.winner_price
            row_idx += 1

        # Суммируем сумму объявления один раз
        if record.sum_amount > 0:
            total_sum += record.sum_amount

    # Итоговая строка
    ws.row_dimensions[row_idx].height = 20
    for col_idx in range(1, len(COL_HEADERS) + 1):
        cell = ws.cell(row=row_idx, column=col_idx)
        cell.fill = PatternFill(fill_type="solid", fgColor="F2F2F2")
        cell.border = THIN_BORDER
        cell.font = SUMMARY_FONT

    lbl = ws.cell(row=row_idx, column=2)
    lbl.value = "ИТОГО:"
    lbl.alignment = Alignment(horizontal="right", vertical="center")

    cell_sum = ws.cell(row=row_idx, column=6)
    cell_sum.value = total_sum
    cell_sum.number_format = NUMBER_FORMAT
    cell_sum.alignment = Alignment(horizontal="right", vertical="center")

    cell_price = ws.cell(row=row_idx, column=12)
    cell_price.value = total_price
    cell_price.number_format = NUMBER_FORMAT
    cell_price.alignment = Alignment(horizontal="right", vertical="center")

    ws.freeze_panes = "A2"


def _write_announcement_row(
    ws,
    row_idx: int,
    record: AnnouncementRecord,
    lot_number: str,
    lot_name: str,
    winner_name: str,
    winner_bin: str,
    winner_price: float,
    is_first_lot: bool = True,
    fill=None,
) -> None:
    """Записывает одну строку (один лот одного объявления)."""

    # A — № (только в первой строке объявления)
    _apply_cell_style(ws.cell(row=row_idx, column=1),
                      record.number if is_first_lot else "", fill=fill)

    # B — Наименование объявления (только в первой строке)
    if is_first_lot:
        if record.name:
            _apply_cell_style(ws.cell(row=row_idx, column=2), record.name, fill=fill)
        elif record.error:
            _apply_cell_style(ws.cell(row=row_idx, column=2),
                              f"⚠ {record.error}", fill=fill)
        else:
            _apply_cell_style(ws.cell(row=row_idx, column=2), "", fill=fill)
    else:
        _apply_cell_style(ws.cell(row=row_idx, column=2), "", fill=fill)

    # C — Способ (только в первой строке)
    _apply_cell_style(ws.cell(row=row_idx, column=3),
                      record.method if is_first_lot else "", fill=fill)

    # D — Начало приема заявок (только в первой строке)
    _apply_cell_style(ws.cell(row=row_idx, column=4),
                      record.start_date if is_first_lot else "", fill=fill)

    # E — Окончание приема заявок (только в первой строке)
    _apply_cell_style(ws.cell(row=row_idx, column=5),
                      record.end_date if is_first_lot else "", fill=fill)

    # F — Сумма закупки (только в первой строке)
    cell_f = ws.cell(row=row_idx, column=6)
    if is_first_lot and record.sum_amount > 0:
        _apply_cell_style(cell_f, record.sum_amount, is_number=True, fill=fill)
    else:
        _apply_cell_style(cell_f, "" if is_first_lot else "—", fill=fill)

    # G — Статус (только в первой строке)
    _apply_cell_style(ws.cell(row=row_idx, column=7),
                      record.status if is_first_lot else "", fill=fill)

    # H — № лота
    _apply_cell_style(ws.cell(row=row_idx, column=8), lot_number or "—", fill=fill)

    # I — Наименование лота
    _apply_cell_style(ws.cell(row=row_idx, column=9),
                      lot_name if lot_name else "—", fill=fill)

    # J — Наименование победителя
    _apply_cell_style(ws.cell(row=row_idx, column=10),
                      winner_name if winner_name else "—", fill=fill)

    # K — БИН победителя
    _apply_cell_style(ws.cell(row=row_idx, column=11),
                      winner_bin if winner_bin else "—", fill=fill)

    # L — Цена победителя
    cell_l = ws.cell(row=row_idx, column=12)
    if record.has_contracts and not winner_price:
        _apply_cell_style(cell_l, "", fill=fill)
    elif winner_price > 0:
        _apply_cell_style(cell_l, winner_price, is_number=True, fill=fill)
    else:
        _apply_cell_style(cell_l, "—", fill=fill)

    # M — Гиперссылка (только в первой строке)
    cell_m = ws.cell(row=row_idx, column=13)
    if is_first_lot and record.url:
        cell_m.value = record.url
        cell_m.hyperlink = record.url
        cell_m.font = LINK_FONT
        cell_m.border = THIN_BORDER
        cell_m.alignment = Alignment(vertical="top", wrap_text=False)
        if fill:
            cell_m.fill = fill
    else:
        _apply_cell_style(cell_m, "", fill=fill)


def build_excel_announcements_report(result: ScrapeAnnouncementsResult) -> bytes:
    wb = Workbook()
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    _write_announcements_sheet(wb, result)

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def get_announcements_report_filename() -> str:
    return f"goszakup_announcements_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"