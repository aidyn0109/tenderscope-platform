"""
worker.py — Автономный процесс парсинга.

Запускается как subprocess из app.py:
    python worker.py <json_input_file> <json_output_file>

Обмен данными через временные JSON-файлы:
  input:  {"bins": ["БИН1", "БИН2"], "progress_file": "path/to/prog.json"}  # режим договоров
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
        "bin":                      rec.bin,
        "contract_number":          rec.contract_number,
        "description":              rec.description,
        "validity_period":          rec.validity_period,
        "amount_final":             rec.amount_final,
        "amount_actual":            rec.amount_actual,
        "difference":               rec.difference,
        "url":                      rec.url,
        "error":                    rec.error,
        "specifics_2026_with_vat":    rec.specifics_2026_with_vat,
        "specifics_2026_without_vat": rec.specifics_2026_without_vat,
    }


def _ann_to_dict(rec: AnnouncementRecord) -> dict:
    return {
        "number":        rec.number,
        "name":          rec.name,
        "method":        rec.method,
        "start_date":    rec.start_date,
        "end_date":      rec.end_date,
        "sum_amount":    rec.sum_amount,
        "status":        rec.status,
        "winner_name":   rec.winner_name,
        "winner_bin":    rec.winner_bin,
        "winner_price":  rec.winner_price,
        "url":           rec.url,
        "has_contracts": rec.has_contracts,
        "error":         rec.error,
        "lots": [
            {
                "lot_number":   lot.lot_number,
                "lot_name":     lot.lot_name,
                "lot_amount":   lot.lot_amount,
                "winner_name":  lot.winner_name,
                "winner_bin":   lot.winner_bin,
                "winner_price": lot.winner_price,
                "year1_sum":    lot.year1_sum,
            }
            for lot in rec.lots
        ],
    }


def _run(bins: list[str], progress_file: str, output_file: str) -> tuple[list[dict], dict]:
    progress = {
        "bin_current":     0,
        "bin_total":       len(bins),
        "bin_name":        "",
        "contract_current": 0,
        "contract_total":  0,
        "message":         "Запуск браузера...",
        "done":            False,
    }
    _write_progress(progress_file, progress)

    all_records: list[dict] = []
    # Сразу создаём пустой output — app.py может читать в любой момент
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
        bins,
        on_bin_start=on_bin_start,
        on_contract_progress=on_contract,
        on_record=on_record,
    )

    return all_records, progress


def _run_announcements(date: str, date_to: str, filter_bin: str | None, progress_file: str, output_file: str) -> tuple[list[dict], dict]:
    progress = {
        "announcement_current": 0,
        "announcement_total":   0,
        "message":              "Запуск браузера...",
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
            # Режим договоров (оригинальный)
            bins = params.get("bins", [])
            log.info("Парсинг договоров для БИН: %s", bins)
            records, progress = _run(bins, progress_file, output_file)

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