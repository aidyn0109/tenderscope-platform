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

HEADER_FONT       = Font(name="Arial", bold=True, size=10, color="000000")
CELL_FONT         = Font(name="Arial", size=10)
LINK_FONT         = Font(name="Arial", size=10, color="0563C1", underline="single")
SUMMARY_FONT      = Font(name="Arial", bold=True, size=10)
HEADER_FILL       = PatternFill(fill_type="solid", fgColor="D9D9D9")
SUMMARY_FILL      = PatternFill(fill_type="solid", fgColor="F2F2F2")
TOTAL_FILL        = PatternFill(fill_type="solid", fgColor="BDD7EE")

THIN_BORDER_SIDE  = Side(style="thin", color="BFBFBF")
THIN_BORDER       = Border(
    left=THIN_BORDER_SIDE,
    right=THIN_BORDER_SIDE,
    top=THIN_BORDER_SIDE,
    bottom=THIN_BORDER_SIDE,
)

NUMBER_FORMAT     = '#,##0.00'
COL_HEADERS       = ["БИН компании", "Краткое содержание", "Разница сумм (тенге)", "Ссылка на договор"]
COL_WIDTHS        = [16, 60, 24, 50]


def _apply_header_style(cell, text: str) -> None:
    cell.value = text
    cell.font = HEADER_FONT
    cell.fill = HEADER_FILL
    cell.border = THIN_BORDER
    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _apply_cell_style(cell, value, is_number: bool = False, is_link: bool = False) -> None:
    cell.value = value
    cell.border = THIN_BORDER
    cell.alignment = Alignment(vertical="top", wrap_text=not is_link)

    if is_link:
        cell.font = LINK_FONT
        cell.alignment = Alignment(vertical="top", wrap_text=False)
    elif is_number:
        cell.font = CELL_FONT
        cell.number_format = NUMBER_FORMAT
        cell.alignment = Alignment(horizontal="right", vertical="top")
    else:
        cell.font = CELL_FONT


def _write_bin_sheet(wb: Workbook, result: ScrapeResult) -> None:
    """Создаёт лист для одного БИН и заполняет его данными."""
    # Название листа — сам БИН (до 31 символа, ограничение Excel)
    sheet_title = result.bin[:31]
    ws = wb.create_sheet(title=sheet_title)

    # ── Заголовки ─────────────────────────────────────────────────────────
    ws.row_dimensions[1].height = 30
    for col_idx, (header, width) in enumerate(zip(COL_HEADERS, COL_WIDTHS), start=1):
        cell = ws.cell(row=1, column=col_idx)
        _apply_header_style(cell, header)
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    # ── Данные ────────────────────────────────────────────────────────────
    for row_idx, record in enumerate(result.records, start=2):
        ws.row_dimensions[row_idx].height = 15

        # A — БИН
        _apply_cell_style(ws.cell(row=row_idx, column=1), record.bin)

        # B — Краткое содержание
        desc = record.description
        if record.error and record.error not in ("Сумма не найдена",):
            desc = f"⚠ {record.error}"
        _apply_cell_style(ws.cell(row=row_idx, column=2), desc)

        # C — Разница сумм
        diff_cell = ws.cell(row=row_idx, column=3)
        if record.error and record.error not in ("Сумма не найдена",):
            _apply_cell_style(diff_cell, "—")
        else:
            _apply_cell_style(diff_cell, record.difference, is_number=True)
            # Подсвечиваем отрицательную разницу красным
            if record.difference < 0:
                diff_cell.font = Font(name="Arial", size=10, color="C00000",
                                      bold=False)

        # D — Ссылка на договор
        link_cell = ws.cell(row=row_idx, column=4)
        if record.url:
            link_cell.value = record.url
            link_cell.hyperlink = record.url
            link_cell.font = LINK_FONT
            link_cell.border = THIN_BORDER
            link_cell.alignment = Alignment(vertical="top", wrap_text=False)
        else:
            _apply_cell_style(link_cell, "—")

    # ── Итоговая строка листа ─────────────────────────────────────────────
    last_row = len(result.records) + 2
    ws.row_dimensions[last_row].height = 18

    total_diff = sum(
        r.difference for r in result.records if not r.error or r.error == "Сумма не найдена"
    )

    total_label = ws.cell(row=last_row, column=2)
    total_label.value = "ИТОГО по БИН:"
    total_label.font = SUMMARY_FONT
    total_label.fill = SUMMARY_FILL
    total_label.border = THIN_BORDER
    total_label.alignment = Alignment(horizontal="right", vertical="center")

    total_val = ws.cell(row=last_row, column=3)
    total_val.value = total_diff
    total_val.font = SUMMARY_FONT
    total_val.fill = SUMMARY_FILL
    total_val.border = THIN_BORDER
    total_val.number_format = NUMBER_FORMAT
    total_val.alignment = Alignment(horizontal="right", vertical="center")

    # Объединяем пустые ячейки итоговой строки
    ws.cell(row=last_row, column=1).fill = SUMMARY_FILL
    ws.cell(row=last_row, column=1).border = THIN_BORDER
    ws.cell(row=last_row, column=4).fill = SUMMARY_FILL
    ws.cell(row=last_row, column=4).border = THIN_BORDER

    # Закрепить первую строку (заголовок)
    ws.freeze_panes = "A2"


def _write_summary_sheet(wb: Workbook, results: list[ScrapeResult]) -> None:
    """Создаёт сводный лист «Сводка» первым в книге."""
    ws = wb.create_sheet(title="Сводка", index=0)

    # Заголовок документа
    ws.merge_cells("A1:D1")
    title_cell = ws.cell(row=1, column=1,
                         value="Сводный отчёт по государственным закупкам")
    title_cell.font = Font(name="Arial", bold=True, size=13)
    title_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 30

    # Дата формирования
    ws.merge_cells("A2:D2")
    date_cell = ws.cell(row=2, column=1,
                        value=f"Сформировано: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
    date_cell.font = Font(name="Arial", size=9, color="808080")
    date_cell.alignment = Alignment(horizontal="center")
    ws.row_dimensions[2].height = 18

    # Заголовки таблицы (строка 4)
    summary_headers = ["БИН компании", "Кол-во договоров", "Кол-во ошибок", "Итоговая разница (тенге)"]
    summary_widths   = [18, 20, 16, 30]

    ws.row_dimensions[4].height = 28
    for col_idx, (header, width) in enumerate(zip(summary_headers, summary_widths), start=1):
        cell = ws.cell(row=4, column=col_idx)
        _apply_header_style(cell, header)
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    # Данные
    grand_total = 0.0
    total_contracts = 0
    total_errors = 0

    for row_idx, result in enumerate(results, start=5):
        bin_diff = sum(
            r.difference for r in result.records
            if not r.error or r.error == "Сумма не найдена"
        )
        err_count = len([r for r in result.records if r.error and r.error != "Сумма не найдена"])
        contract_count = len(result.records)

        grand_total     += bin_diff
        total_contracts += contract_count
        total_errors    += err_count

        ws.row_dimensions[row_idx].height = 16

        bin_cell = ws.cell(row=row_idx, column=1, value=result.bin)
        bin_cell.font = CELL_FONT
        bin_cell.border = THIN_BORDER
        bin_cell.alignment = Alignment(horizontal="center", vertical="center")

        cnt_cell = ws.cell(row=row_idx, column=2, value=contract_count)
        cnt_cell.font = CELL_FONT
        cnt_cell.border = THIN_BORDER
        cnt_cell.alignment = Alignment(horizontal="center", vertical="center")

        err_cell = ws.cell(row=row_idx, column=3, value=err_count)
        err_cell.font = CELL_FONT
        err_cell.border = THIN_BORDER
        err_cell.alignment = Alignment(horizontal="center", vertical="center")
        if err_count > 0:
            err_cell.font = Font(name="Arial", size=10, color="C00000")

        diff_cell = ws.cell(row=row_idx, column=4, value=bin_diff)
        diff_cell.font = CELL_FONT
        diff_cell.border = THIN_BORDER
        diff_cell.number_format = NUMBER_FORMAT
        diff_cell.alignment = Alignment(horizontal="right", vertical="center")
        if bin_diff < 0:
            diff_cell.font = Font(name="Arial", size=10, color="C00000")

    # Итоговая строка
    total_row = len(results) + 5
    ws.row_dimensions[total_row].height = 20

    labels = ["ИТОГО", str(total_contracts), str(total_errors), None]
    for col_idx, label in enumerate(labels, start=1):
        cell = ws.cell(row=total_row, column=col_idx)
        cell.fill = TOTAL_FILL
        cell.border = THIN_BORDER
        cell.font = Font(name="Arial", bold=True, size=10)
        cell.alignment = Alignment(horizontal="center", vertical="center")
        if label is not None:
            cell.value = label

    grand_cell = ws.cell(row=total_row, column=4, value=grand_total)
    grand_cell.fill = TOTAL_FILL
    grand_cell.border = THIN_BORDER
    grand_cell.font = Font(name="Arial", bold=True, size=10,
                           color="C00000" if grand_total < 0 else "000000")
    grand_cell.number_format = NUMBER_FORMAT
    grand_cell.alignment = Alignment(horizontal="right", vertical="center")

    ws.freeze_panes = "A5"


def build_excel_report(results: list[ScrapeResult]) -> bytes:
    """
    Формирует Excel-книгу по результатам парсинга.

    Структура:
      - Лист «Сводка» (первый) — агрегированные итоги по каждому БИН
      - По одному листу на каждый БИН с детализацией по договорам

    Возвращает байты .xlsx файла, готовые к отдаче через st.download_button.
    """
    wb = Workbook()
    # Удаляем дефолтный лист
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    # Сначала создаём листы БИН
    for result in results:
        _write_bin_sheet(wb, result)

    # Потом сводку (она вставит себя на index=0)
    _write_summary_sheet(wb, results)

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def get_report_filename() -> str:
    """Возвращает имя файла с временной меткой."""
    return f"goszakup_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"