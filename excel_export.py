"""
excel_export.py — Формирование Excel-отчёта по результатам парсинга госзакупок
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

from scraper import ScrapeResult, ContractRecord

# ── Стили ──────────────────────────────────────────────────────────────────

HEADER_FONT   = Font(name="Arial", bold=True, size=10, color="000000")
CELL_FONT     = Font(name="Arial", size=10)
LINK_FONT     = Font(name="Arial", size=10, color="0563C1", underline="single")
SUMMARY_FONT  = Font(name="Arial", bold=True, size=10)
HEADER_FILL   = PatternFill(fill_type="solid", fgColor="D9D9D9")
SUMMARY_FILL  = PatternFill(fill_type="solid", fgColor="F2F2F2")
TOTAL_FILL    = PatternFill(fill_type="solid", fgColor="BDD7EE")
RED_FONT      = Font(name="Arial", size=10, color="C00000")
RED_BOLD_FONT = Font(name="Arial", bold=True, size=10, color="C00000")

THIN_SIDE   = Side(style="thin", color="BFBFBF")
THIN_BORDER = Border(left=THIN_SIDE, right=THIN_SIDE, top=THIN_SIDE, bottom=THIN_SIDE)

NUMBER_FORMAT = "#,##0.00"

# Столбцы листа "Договоры"
COL_HEADERS = [
    "БИН компании",
    "Номер договора",
    "Краткое содержание",
    "Срок действия договора",
    "Общая итоговая сумма (тг)",
    "Общая фактическая сумма (тг)",
    "Разница (тг)",
    "Специфика на 2026 год с НДС (тг)",
    "Специфика на 2026 год без НДС (тг)",
    "Ссылка на договор",
]
COL_WIDTHS = [16, 28, 55, 22, 26, 28, 22, 30, 30, 50]

# Индексы числовых столбцов (1-based): E=5, F=6, G=7, H=8, I=9
NUMERIC_COLS = {5, 6, 7, 8, 9}
# Индекс столбца ссылок
LINK_COL = 10


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


def _write_contracts_sheet(wb: Workbook, results: list[ScrapeResult]) -> None:
    """Единый лист «Договоры» со всеми записями по всем БИН."""
    ws = wb.create_sheet(title="Договоры")

    # Заголовки
    ws.row_dimensions[1].height = 32
    for col_idx, (header, width) in enumerate(zip(COL_HEADERS, COL_WIDTHS), start=1):
        cell = ws.cell(row=1, column=col_idx)
        _apply_header_style(cell, header)
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    row_idx = 2
    for result in results:
        for record in result.records:
            ws.row_dimensions[row_idx].height = 15

            has_error = bool(record.error and record.error not in ("Сумма не найдена",))

            # A — БИН
            _apply_cell_style(ws.cell(row=row_idx, column=1), record.bin)

            # B — Номер договора
            _apply_cell_style(ws.cell(row=row_idx, column=2), record.contract_number or "")

            # C — Краткое содержание
            desc = f"⚠ {record.error}" if has_error else record.description
            _apply_cell_style(ws.cell(row=row_idx, column=3), desc)

            # D — Срок действия
            _apply_cell_style(ws.cell(row=row_idx, column=4), record.validity_period or "")

            # E — Общая итоговая сумма
            cell_e = ws.cell(row=row_idx, column=5)
            if has_error:
                _apply_cell_style(cell_e, "—")
            else:
                _apply_cell_style(cell_e, record.amount_final, is_number=True)

            # F — Общая фактическая сумма
            cell_f = ws.cell(row=row_idx, column=6)
            if has_error:
                _apply_cell_style(cell_f, "—")
            else:
                _apply_cell_style(cell_f, record.amount_actual, is_number=True)

            # G — Разница
            cell_g = ws.cell(row=row_idx, column=7)
            if has_error:
                _apply_cell_style(cell_g, "—")
            else:
                _apply_cell_style(cell_g, record.difference, is_number=True)
                if record.difference < 0:
                    cell_g.font = RED_FONT

            # H — Специфика 2026 с НДС
            cell_h = ws.cell(row=row_idx, column=8)
            if has_error:
                _apply_cell_style(cell_h, "—")
            elif record.specifics_2026_with_vat > 0:
                _apply_cell_style(cell_h, record.specifics_2026_with_vat, is_number=True)
            else:
                _apply_cell_style(cell_h, "—")

            # I — Специфика 2026 без НДС
            cell_i = ws.cell(row=row_idx, column=9)
            if has_error:
                _apply_cell_style(cell_i, "—")
            elif record.specifics_2026_without_vat > 0:
                _apply_cell_style(cell_i, record.specifics_2026_without_vat, is_number=True)
            else:
                _apply_cell_style(cell_i, "—")

            # J — Ссылка
            cell_j = ws.cell(row=row_idx, column=10)
            if record.url:
                cell_j.value = record.url
                cell_j.hyperlink = record.url
                cell_j.font = LINK_FONT
                cell_j.border = THIN_BORDER
                cell_j.alignment = Alignment(vertical="top", wrap_text=False)
            else:
                _apply_cell_style(cell_j, "—")

            row_idx += 1

    # Итоговая строка внизу таблицы
    ws.row_dimensions[row_idx].height = 20

    total_final  = sum(
        r.amount_final for res in results for r in res.records
        if not r.error or r.error == "Сумма не найдена"
    )
    total_actual = sum(
        r.amount_actual for res in results for r in res.records
        if not r.error or r.error == "Сумма не найдена"
    )
    total_diff = total_final - total_actual
    total_spec_with = sum(
        r.specifics_2026_with_vat for res in results for r in res.records
        if not r.error or r.error == "Сумма не найдена"
    )
    total_spec_without = sum(
        r.specifics_2026_without_vat for res in results for r in res.records
        if not r.error or r.error == "Сумма не найдена"
    )

    for col_idx in range(1, len(COL_HEADERS) + 1):
        cell = ws.cell(row=row_idx, column=col_idx)
        cell.fill = SUMMARY_FILL
        cell.border = THIN_BORDER
        cell.font = SUMMARY_FONT

    lbl = ws.cell(row=row_idx, column=3)
    lbl.value = "ИТОГО:"
    lbl.alignment = Alignment(horizontal="right", vertical="center")

    for col_idx, val in [
        (5, total_final),
        (6, total_actual),
        (7, total_diff),
        (8, total_spec_with),
        (9, total_spec_without),
    ]:
        cell = ws.cell(row=row_idx, column=col_idx)
        cell.value = val
        cell.number_format = NUMBER_FORMAT
        cell.alignment = Alignment(horizontal="right", vertical="center")
        if col_idx == 7 and val < 0:
            cell.font = RED_BOLD_FONT

    ws.freeze_panes = "A2"


def _write_summary_sheet(wb: Workbook, results: list[ScrapeResult]) -> None:
    """Лист «Сводка» — агрегированные итоги по каждому БИН."""
    ws = wb.create_sheet(title="Сводка", index=0)

    ws.merge_cells("A1:G1")
    title_cell = ws.cell(row=1, column=1,
                         value="Сводный отчёт по государственным закупкам")
    title_cell.font = Font(name="Arial", bold=True, size=13)
    title_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 30

    ws.merge_cells("A2:G2")
    date_cell = ws.cell(
        row=2, column=1,
        value=f"Сформировано: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}"
    )
    date_cell.font = Font(name="Arial", size=9, color="808080")
    date_cell.alignment = Alignment(horizontal="center")
    ws.row_dimensions[2].height = 18

    summary_headers = [
        "БИН компании",
        "Кол-во договоров",
        "Кол-во ошибок",
        "Итоговая сумма (тг)",
        "Фактическая сумма (тг)",
        "Разница (тг)",
        "Специфика 2026 с НДС (тг)",
        "Специфика 2026 без НДС (тг)",
    ]
    summary_widths = [18, 20, 16, 26, 26, 24, 28, 28]

    ws.row_dimensions[4].height = 28
    for col_idx, (header, width) in enumerate(zip(summary_headers, summary_widths), start=1):
        cell = ws.cell(row=4, column=col_idx)
        _apply_header_style(cell, header)
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    grand_final  = 0.0
    grand_actual = 0.0
    grand_spec_with = 0.0
    grand_spec_without = 0.0
    total_contracts = 0
    total_errors    = 0

    for row_idx, result in enumerate(results, start=5):
        valid_records = [r for r in result.records if not r.error or r.error == "Сумма не найдена"]
        bin_final  = sum(r.amount_final  for r in valid_records)
        bin_actual = sum(r.amount_actual for r in valid_records)
        bin_diff   = bin_final - bin_actual
        bin_spec_with    = sum(r.specifics_2026_with_vat    for r in valid_records)
        bin_spec_without = sum(r.specifics_2026_without_vat for r in valid_records)
        err_count  = len([r for r in result.records if r.error and r.error != "Сумма не найдена"])

        grand_final        += bin_final
        grand_actual       += bin_actual
        grand_spec_with    += bin_spec_with
        grand_spec_without += bin_spec_without
        total_contracts    += len(result.records)
        total_errors       += err_count

        ws.row_dimensions[row_idx].height = 16

        def _sc(col, val, *, num=False, err=False):
            c = ws.cell(row=row_idx, column=col, value=val)
            c.border = THIN_BORDER
            c.font = RED_FONT if err else CELL_FONT
            if num:
                c.number_format = NUMBER_FORMAT
                c.alignment = Alignment(horizontal="right", vertical="center")
            else:
                c.alignment = Alignment(horizontal="center", vertical="center")

        _sc(1, result.bin)
        _sc(2, len(result.records))
        _sc(3, err_count, err=err_count > 0)
        _sc(4, bin_final,  num=True)
        _sc(5, bin_actual, num=True)
        diff_cell = ws.cell(row=row_idx, column=6, value=bin_diff)
        diff_cell.border = THIN_BORDER
        diff_cell.number_format = NUMBER_FORMAT
        diff_cell.alignment = Alignment(horizontal="right", vertical="center")
        diff_cell.font = RED_FONT if bin_diff < 0 else CELL_FONT
        _sc(7, bin_spec_with    if bin_spec_with    > 0 else "—", num=bin_spec_with    > 0)
        _sc(8, bin_spec_without if bin_spec_without > 0 else "—", num=bin_spec_without > 0)

    # Итоговая строка
    total_row = len(results) + 5
    ws.row_dimensions[total_row].height = 20

    grand_diff = grand_final - grand_actual

    for col_idx in range(1, len(summary_headers) + 1):
        c = ws.cell(row=total_row, column=col_idx)
        c.fill = TOTAL_FILL
        c.border = THIN_BORDER
        c.font = Font(name="Arial", bold=True, size=10)
        c.alignment = Alignment(horizontal="center", vertical="center")

    ws.cell(row=total_row, column=1).value = "ИТОГО"
    ws.cell(row=total_row, column=2).value = str(total_contracts)
    ws.cell(row=total_row, column=3).value = str(total_errors)

    for col_idx, val in [
        (4, grand_final),
        (5, grand_actual),
        (6, grand_diff),
        (7, grand_spec_with),
        (8, grand_spec_without),
    ]:
        c = ws.cell(row=total_row, column=col_idx)
        c.value = val if val > 0 or col_idx <= 6 else "—"
        c.fill = TOTAL_FILL
        c.border = THIN_BORDER
        if isinstance(val, float) and (val > 0 or col_idx <= 6):
            c.number_format = NUMBER_FORMAT
            c.alignment = Alignment(horizontal="right", vertical="center")
        else:
            c.alignment = Alignment(horizontal="center", vertical="center")
        c.font = Font(
            name="Arial", bold=True, size=10,
            color="C00000" if (col_idx == 6 and val < 0) else "000000"
        )

    ws.freeze_panes = "A5"


def build_excel_report(results: list[ScrapeResult]) -> bytes:
    """
    Формирует Excel-книгу по результатам парсинга.

    Структура:
      - Лист «Сводка» (первый) — агрегированные итоги по каждому БИН
      - Лист «Договоры» — единая таблица всех договоров по всем БИН

    Возвращает байты .xlsx файла для st.download_button.
    """
    wb = Workbook()
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    _write_contracts_sheet(wb, results)
    _write_summary_sheet(wb, results)

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def get_report_filename() -> str:
    return f"goszakup_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"