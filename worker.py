"""
worker.py — Автономный процесс парсинга.

Запускается как subprocess из app.py:
    python worker.py <json_input_file> <json_output_file>

Обмен данными через временные JSON-файлы:
  input:  {"bins": [{"bin": "БИН1", "max_income": 500000000.0}, ...], "progress_file": "path/to/prog.json"}  # режим договоров
          {"mode": "announcements", "date": "YYYY-MM-DD", "progress_file": "path/to/prog.json"}  # режим объявлений
  output: {"records": [...], "error": null}

Ключевое свойство: output.json пишется ПОСЛЕ КАЖДОГО договора/объявления через on_record callback.
Если воркер убьют (OOM), app.py найдёт частичные данные и покажет их пользователю.
done=True пишется только после полного завершения.
"""

import json
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WORKER] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger(__name__)

from scraper import ContractRecord, ScrapeResult, scrape_all
from scraper_announcements import AnnouncementRecord, scrape_announcements


def _write_progress(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


def _write_output(path: str, records: list) -> None:
    """Атомарная запись output.json — tmp→rename, чтобы app.py не прочитал обрезанный файл."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"records": records, "error": None}, f,
                      ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        log.warning("Ошибка записи output: %s", e)


def _rec_to_dict(rec: ContractRecord) -> dict:
    return {
        "bin":                 rec.bin,
        "supplier_name":       rec.supplier_name,
        "contract_number":     rec.contract_number,
        "description":         rec.description,
        "cr_datetime":         rec.cr_datetime,
        "amount_planned":      rec.amount_planned,
        "amount_actual":       rec.amount_actual,
        "amount_total":        rec.amount_total,
        "max_income":          rec.max_income,
        "url":                 rec.url,
        "error":               rec.error,
    }


def _ann_to_dict(rec: AnnouncementRecord) -> dict:
    return {
        "bin":                  rec.bin,
        "supplier_name":        rec.supplier_name,
        "announcement_number":  rec.announcement_number,
        "announcement_name":    rec.announcement_name,
        "year1_sum":            rec.year1_sum,
        "protocol_url":         rec.protocol_url,
        "announcement_url":     rec.announcement_url,
        "error":                rec.error,
    }


def _run(bin_data: list[dict], progress_file: str, output_file: str) -> tuple[list[dict], dict]:
    """
    bin_data: список словарей вида {"bin": "031240001439", "max_income": 500000000.0}
    """
    progress = {
        "bin_current":     0,
        "bin_total":       len(bin_data),
        "bin_name":        "",
        "contract_current": 0,
        "contract_total":  0,
        "message":         "Запуск сбора данных...",
        "done":            False,
    }
    _write_progress(progress_file, progress)

    all_records: list[dict] = []
    _write_output(output_file, all_records)

    def on_bin_start(cur: int, tot: int, name: str) -> None:
        log.info("БИН %s (%d/%d)", name, cur, tot)
        progress.update(
            bin_current=cur, bin_total=tot, bin_name=name,
            contract_current=0, contract_total=0,
            message=f"Обрабатываем БИН {name}...",
        )
        _write_progress(progress_file, progress)

    def on_contract(cur: int, tot: int, msg: str) -> None:
        log.info("  договор %d/%d", cur, tot)
        progress.update(contract_current=cur, contract_total=tot, message=msg)
        _write_progress(progress_file, progress)

    def on_record(rec: ContractRecord) -> None:
        """Вызывается сразу после парсинга каждого договора — сохраняем на диск."""
        all_records.append(_rec_to_dict(rec))
        _write_output(output_file, all_records)
        log.info("  сохранено %d записей", len(all_records))

    scrape_all(
        bin_data,
        on_bin_start=on_bin_start,
        on_contract_progress=on_contract,
        on_record=on_record,
    )

    return all_records, progress


def _run_announcements(date: str, date_to: str, filter_bin: str | None, progress_file: str, output_file: str) -> tuple[list[dict], dict]:
    progress = {
        "announcement_current": 0,
        "announcement_total":   0,
        "message":              "Запуск сбора данных...",
        "done":                 False,
    }
    _write_progress(progress_file, progress)

    all_records: list[dict] = []
    _write_output(output_file, all_records)

    def on_progress(cur: int, tot: int, msg: str) -> None:
        log.info("  объявление %d/%d", cur, tot)
        progress.update(announcement_current=cur, announcement_total=tot, message=msg)
        _write_progress(progress_file, progress)

    def on_record(rec: AnnouncementRecord) -> None:
        """Вызывается сразу после парсинга каждого объявления — сохраняем на диск."""
        all_records.append(_ann_to_dict(rec))
        _write_output(output_file, all_records)
        log.info("  сохранено %d записей", len(all_records))

    result = scrape_announcements(date, on_progress=on_progress, on_record=on_record, date_to=date_to, filter_bin=filter_bin)

    return all_records, progress


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: python worker.py <input.json> <output.json>", file=sys.stderr)
        sys.exit(1)

    input_file  = sys.argv[1]
    output_file = sys.argv[2]

    with open(input_file, encoding="utf-8") as f:
        params = json.load(f)

    progress_file = params.get("progress_file")
    mode = params.get("mode", "contracts")  # по умолчанию режим договоров

    log.info("Старт. Режим: %s", mode)

    try:
        if mode == "announcements":
            date = params.get("date")
            date_to = params.get("date_to", date)
            filter_bin = params.get("filter_bin")
            log.info("Парсинг объявлений: %s — %s, БИН фильтр: %s", date, date_to, filter_bin or "нет")
            records, progress = _run_announcements(date, date_to, filter_bin, progress_file, output_file)
        else:
            # Режим договоров — bins теперь список словарей [{bin, max_income}, ...]
            bin_data = params.get("bins", [])
            # Совместимость: если старый формат (список строк), конвертируем
            if bin_data and isinstance(bin_data[0], str):
                bin_data = [{"bin": b, "max_income": 0.0} for b in bin_data]
            bin_names = [b["bin"] for b in bin_data]
            log.info("Парсинг договоров для БИН: %s", bin_names)
            records, progress = _run(bin_data, progress_file, output_file)

        # Финальная запись — фиксируем итоговый список (on_record уже писал частично)
        _write_output(output_file, records)
        log.info("Результат финализирован: %d записей", len(records))
    except Exception as exc:
        log.exception("Критическая ошибка: %s", exc)
        # output.json уже содержит частичные данные от on_record — не затираем его
        progress = {
            "announcement_current": 0, "announcement_total": 0,
            "bin_current": 0, "bin_total": 0,
            "bin_name": "", "contract_current": 0, "contract_total": 0,
            "message": f"Ошибка: {exc}", "done": False,
        }

    # Сигнализируем о завершении (done=True) только после записи output
    progress.update(done=True, message="Готово!")
    _write_progress(progress_file, progress)
    log.info("done=True записан в %s", progress_file)


if __name__ == "__main__":
    main()