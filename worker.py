"""
worker.py — Автономный процесс парсинга.

Запускается как subprocess из app.py:
    python worker.py <json_input_file> <json_output_file>

Обмен данными через временные JSON-файлы:
  input:  {"bins": ["БИН1", "БИН2"], "progress_file": "path/to/prog.json"}
  output: {"records": [...], "error": null}

ВАЖНО: done=True пишется в progress.json только ПОСЛЕ записи output.json,
чтобы исключить race condition при чтении со стороны app.py.
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

from scraper import ScrapeResult, scrape_all


def _write_progress(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


def _run(bins: list[str], progress_file: str) -> list[dict]:
    progress = {
        "bin_current": 0,
        "bin_total": len(bins),
        "bin_name": "",
        "contract_current": 0,
        "contract_total": 0,
        "message": "Запуск браузера...",
        "done": False,
    }
    _write_progress(progress_file, progress)

    def on_bin_start(cur, tot, name):
        log.info("БИН %s (%d/%d)", name, cur, tot)
        progress.update(
            bin_current=cur, bin_total=tot, bin_name=name,
            contract_current=0, contract_total=0,
            message=f"Обрабатываем БИН {name}...",
        )
        _write_progress(progress_file, progress)

    def on_contract(cur, tot, msg):
        log.info("  договор %d/%d", cur, tot)
        progress.update(contract_current=cur, contract_total=tot, message=msg)
        _write_progress(progress_file, progress)

    results: list[ScrapeResult] = scrape_all(
        bins,
        on_bin_start=on_bin_start,
        on_contract_progress=on_contract,
    )

    records = []
    for sr in results:
        for rec in sr.records:
            records.append({
                "bin":                rec.bin,
                "description":        rec.description,
                "amount_procurement": rec.amount_procurement,
                "amount_final":       rec.amount_final,
                "difference":         rec.difference,
                "url":                rec.url,
                "error":              rec.error,
            })

    return records, progress


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: python worker.py <input.json> <output.json>", file=sys.stderr)
        sys.exit(1)

    input_file  = sys.argv[1]
    output_file = sys.argv[2]

    with open(input_file, encoding="utf-8") as f:
        params = json.load(f)

    bins          = params["bins"]
    progress_file = params["progress_file"]

    log.info("Старт. БИН: %s", bins)

    try:
        records, progress = _run(bins, progress_file)
        result = {"records": records, "error": None}
    except Exception as exc:
        log.exception("Критическая ошибка: %s", exc)
        result = {"records": [], "error": str(exc)}
        progress = {
            "bin_current": 0, "bin_total": len(bins),
            "bin_name": "", "contract_current": 0, "contract_total": 0,
            "message": f"Ошибка: {exc}", "done": False,
        }

    # Сначала записываем результаты — потом done=True,
    # чтобы app.py не прочитал done=True раньше чем появился output.json.
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    log.info("Результат записан в %s", output_file)

    # Теперь сигнализируем о завершении
    progress.update(done=True, message="Готово!")
    _write_progress(progress_file, progress)
    log.info("done=True записан в %s", progress_file)


if __name__ == "__main__":
    main()
