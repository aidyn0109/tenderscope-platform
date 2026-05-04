"""
app.py — Streamlit-интерфейс платформы анализа госзакупок
Запуск: streamlit run app.py
"""

import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

from auth import check_credentials
from excel_export import build_excel_report, get_report_filename
from scraper import ContractRecord, ScrapeResult

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

st.set_page_config(
    page_title="TenderScope",
    page_icon="🔍",
    layout="centered",
    initial_sidebar_state="auto",
)

st.markdown("""
<style>
    .main-title { font-size:1.8rem; font-weight:700; color:#1a3a5c; margin-bottom:0.2rem; }
    .subtitle   { color:#666; font-size:0.95rem; margin-bottom:1.5rem; }
    .login-box  { max-width:380px; margin:4rem auto 0; }
    .stButton > button { border-radius:6px; }
    div[data-testid="stDownloadButton"] button {
        background-color:#1a3a5c; color:white;
        font-size:1rem; padding:0.6rem 1.5rem; border-radius:6px;
    }
</style>
""", unsafe_allow_html=True)

WORKER_PATH = Path(__file__).parent / "worker.py"
WORKER_TIMEOUT = 600  # секунд до принудительного таймаута


# ── Валидация ─────────────────────────────────────────────────────────────

def validate_bin(s: str) -> bool:
    return bool(re.fullmatch(r"\d{12}", s.strip()))


def _kill_worker_process(pid: int) -> None:
    """Убивает воркер и все его дочерние процессы (включая Chromium)."""
    try:
        if os.name == "nt":
            # Windows: taskkill /T убивает дерево процессов
            subprocess.call(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            # Linux/Render: убиваем всю группу процессов через SIGKILL
            # preexec_fn=os.setsid при запуске сделал воркер лидером группы
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # процесс уже завершён
    except Exception as e:
        log.warning("Ошибка при остановке воркера PID=%s: %s", pid, e)


def _stop_worker() -> None:
    pid = st.session_state.get("worker_pid")
    if pid:
        _kill_worker_process(pid)
    st.session_state.running    = False
    st.session_state.worker_pid = None
    st.session_state.results    = None


# ── Вспомогательные функции ───────────────────────────────────────────────

def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _records_to_results(records: list[dict]) -> list[ScrapeResult]:
    by_bin: dict[str, ScrapeResult] = {}
    for r in records:
        b = r["bin"]
        if b not in by_bin:
            by_bin[b] = ScrapeResult(bin=b)
        cr = ContractRecord(
            bin=r["bin"],
            contract_number=r.get("contract_number", ""),
            description=r.get("description", ""),
            validity_period=r.get("validity_period", ""),
            amount_final=r.get("amount_final", 0.0),
            amount_actual=r.get("amount_actual", 0.0),
            difference=r.get("difference", 0.0),
            url=r.get("url", ""),
            error=r.get("error", ""),
        )
        by_bin[b].records.append(cr)
        if cr.error:
            by_bin[b].errors.append(cr.error)
    return list(by_bin.values())


# ── Инициализация session_state ───────────────────────────────────────────

def _init_state():
    defaults = {
        "authenticated":     False,
        "current_user":      None,
        "bin_list":          [""],
        "results":           None,
        "excel_bytes":       None,
        "running":           False,
        "tmp_dir":           None,
        "progress_file":     None,
        "output_file":       None,
        "log_file_path":     None,
        "bins_to_process":   [],
        "worker_started_at": 0.0,
        "worker_pid":        None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init_state()


# ──────────────────────────────────────────────────────────────────────────
# СТРАНИЦА 0: Авторизация
# ──────────────────────────────────────────────────────────────────────────

if not st.session_state.authenticated:
    st.markdown('<div class="main-title">TenderScope</div>', unsafe_allow_html=True)
    st.markdown('<div class="subtitle">Платформа для анализа реестра договоров c портала goszakup.gov.kz</div>',
                unsafe_allow_html=True)
    st.divider()

    col_l, col_c, col_r = st.columns([1, 2, 1])
    with col_c:
        st.markdown("#### Вход в систему")
        with st.form("login_form"):
            username = st.text_input("Логин", placeholder="Введите логин")
            password = st.text_input("Пароль", placeholder="Введите пароль", type="password")
            submitted = st.form_submit_button("Войти", use_container_width=True, type="primary")

        if submitted:
            user = check_credentials(username, password)
            if user:
                st.session_state.authenticated = True
                st.session_state.current_user  = user
                log.info("Вход: %s (%s)", user["username"], user["role"])
                st.rerun()
            else:
                st.error("Неверный логин или пароль")

    st.stop()


# ── Сайдбар (показывается только авторизованным) ──────────────────────────

user = st.session_state.current_user
with st.sidebar:
    st.markdown(f"**{user['display_name']}**")
    st.caption(f"Роль: {user['role']}")
    st.divider()
    if st.button("Выйти", use_container_width=True):
        for key in list(st.session_state.keys()):
            del st.session_state[key]
        st.rerun()


# ── Заголовок ─────────────────────────────────────────────────────────────

st.markdown('<div class="main-title">TenderScope</div>', unsafe_allow_html=True)
st.markdown('<div class="subtitle">Платформа для анализа реестра договоров c портала goszakup.gov.kz</div>',
            unsafe_allow_html=True)
st.divider()


# ──────────────────────────────────────────────────────────────────────────
# СТРАНИЦА 1: Форма ввода
# ──────────────────────────────────────────────────────────────────────────

if not st.session_state.running and st.session_state.results is None:
    st.markdown("#### Введите БИН компаний-поставщиков")

    bin_list = st.session_state.bin_list
    for i in range(len(bin_list)):
        col_in, col_rm = st.columns([5, 1])
        with col_in:
            val = st.text_input(
                f"БИН {i+1}", value=bin_list[i], key=f"bin_{i}",
                placeholder="000000000000 (12 цифр)", max_chars=12,
                label_visibility="collapsed",
            )
            bin_list[i] = val.strip()
            if val.strip() and not validate_bin(val):
                st.caption("⚠️ БИН должен содержать ровно 12 цифр")
        with col_rm:
            if len(bin_list) > 1:
                if st.button("✕", key=f"rm_{i}"):
                    bin_list.pop(i); st.rerun()
            else:
                st.write("")

    col_add, _ = st.columns([2, 5])
    with col_add:
        if st.button("＋ Добавить БИН", use_container_width=True):
            bin_list.append(""); st.rerun()

    st.session_state.bin_list = bin_list
    st.divider()

    filled = [b for b in bin_list if b.strip()]
    ok     = bool(filled) and all(validate_bin(b) for b in filled)

    if st.button("🔍 Запустить анализ", disabled=not ok, type="primary", use_container_width=True):
        tmp_dir       = tempfile.mkdtemp(prefix="goszakup_")
        input_file    = os.path.join(tmp_dir, "input.json")
        output_file   = os.path.join(tmp_dir, "output.json")
        progress_file = os.path.join(tmp_dir, "progress.json")
        log_file_path = os.path.join(tmp_dir, "worker.log")

        with open(input_file, "w", encoding="utf-8") as f:
            json.dump({"bins": filled, "progress_file": progress_file}, f, ensure_ascii=False)

        log_file = open(log_file_path, "w", encoding="utf-8")
        popen_kwargs: dict = {
            "stdout": log_file,
            "stderr": sys.stderr,  # ошибки видны в Render-логах
        }
        if os.name != "nt":
            # Linux: запускаем в новой группе процессов →
            # при kill через os.killpg умирает весь Chromium вместе с воркером
            popen_kwargs["preexec_fn"] = os.setsid

        proc = subprocess.Popen(
            [sys.executable, "-u", str(WORKER_PATH), input_file, output_file],
            **popen_kwargs,
        )
        log.info("Воркер запущен PID=%d  tmp=%s", proc.pid, tmp_dir)

        st.session_state.running           = True
        st.session_state.worker_pid        = proc.pid
        st.session_state.tmp_dir           = tmp_dir
        st.session_state.progress_file     = progress_file
        st.session_state.output_file       = output_file
        st.session_state.log_file_path     = log_file_path
        st.session_state.bins_to_process   = filled
        st.session_state.results           = None
        st.session_state.excel_bytes       = None
        st.session_state.worker_started_at = time.time()
        st.rerun()


# ──────────────────────────────────────────────────────────────────────────
# СТРАНИЦА 2: Прогресс (file-based polling — без blocking while-loop)
# ──────────────────────────────────────────────────────────────────────────

if st.session_state.running:
    progress_file = st.session_state.progress_file
    output_file   = st.session_state.output_file
    n_bins        = len(st.session_state.bins_to_process)
    started_at    = st.session_state.worker_started_at

    st.markdown("#### 📡 Идёт сбор данных...")

    prog   = _read_json(progress_file) if os.path.exists(progress_file) else {}
    b_name = prog.get("bin_name", "")
    b_cur  = prog.get("bin_current", 0)
    b_tot  = prog.get("bin_total",   n_bins)
    c_cur  = prog.get("contract_current", 0)
    c_tot  = prog.get("contract_total",   0)
    msg    = prog.get("message", "Запуск браузера Chromium...")
    done   = prog.get("done", False)

    if b_name:
        st.markdown(f"Обработка БИН **{b_name}** &nbsp; `{b_cur} / {b_tot}`")
    else:
        st.markdown(f"⏳ {msg}")

    st.progress(b_cur / max(b_tot, 1), text=f"Прогресс по БИН: {b_cur}/{b_tot}")
    if c_tot > 0:
        st.progress(c_cur / c_tot, text=f"Договор {c_cur} из {c_tot}")
    st.caption(msg)

    if st.button("⛔ Остановить сбор данных", type="secondary"):
        _stop_worker()
        st.rerun()

    # ── Проверка завершения ────────────────────────────────────────────────
    output_ready = os.path.exists(output_file)

    if done and output_ready:
        error   = None
        results = []
        try:
            data    = _read_json(output_file)
            error   = data.get("error")
            all_recs = data.get("records", [])
            results = _records_to_results(all_recs)
            total_loaded = sum(len(r.records) for r in results)
            log.info("Загружено %d записей", total_loaded)

            # Выводим ошибки парсинга в лог Render для диагностики
            err_recs = [r for r in all_recs if r.get("error") and r.get("error") != "Сумма не найдена"]
            if err_recs:
                log.warning("Ошибок при парсинге: %d из %d", len(err_recs), total_loaded)
                for r in err_recs[:30]:
                    log.warning("  БИН %s | %s | %s",
                                r.get("bin", "?"),
                                r.get("url", "")[-70:],
                                r.get("error", "?"))
        except Exception as exc:
            error = f"Ошибка чтения результата: {exc}"
            log.exception(error)

        excel_bytes = None
        if results:
            try:
                excel_bytes = build_excel_report(results)
            except Exception as exc:
                log.exception("Ошибка Excel: %s", exc)

        st.session_state.worker_error = error
        st.session_state.results     = results
        st.session_state.excel_bytes = excel_bytes
        st.session_state.running     = False

        if error:
            st.error(f"❌ {error}")

        st.rerun()

    elif done and not output_ready:
        time.sleep(0.5)
        st.rerun()

    elif time.time() - started_at > WORKER_TIMEOUT:
        st.error(f"❌ Превышено время ожидания ({WORKER_TIMEOUT // 60} мин). Проверьте интернет-соединение.")
        st.session_state.running = False
        st.rerun()

    else:
        time.sleep(1)
        st.rerun()


# ──────────────────────────────────────────────────────────────────────────
# СТРАНИЦА 3: Результаты
# ──────────────────────────────────────────────────────────────────────────

if st.session_state.results is not None and not st.session_state.running:
    results: list[ScrapeResult] = st.session_state.results

    if not results:
        worker_err = st.session_state.get("worker_error")
        if worker_err:
            st.error(f"❌ Ошибка: {worker_err}")
        log_path = st.session_state.get("log_file_path")
        if log_path and os.path.exists(log_path):
            with open(log_path, encoding="utf-8") as f:
                st.code(f.read()[-3000:], language="text")
        else:
            st.error("❌ Данные не получены.")
    else:
        st.success("✅ Анализ успешно завершён!")
        st.markdown("#### 📊 Результаты")

        total_contracts = sum(len(r.records) for r in results)
        total_errors    = sum(len(r.errors)  for r in results)
        grand_diff      = sum(
            rec.difference for r in results for rec in r.records
            if not rec.error or rec.error == "Сумма не найдена"
        )

        c1, c2, c3 = st.columns(3)
        c1.metric("Всего договоров",   total_contracts)
        c2.metric("Ошибок при сборе",  total_errors)
        c3.metric("Суммарная разница", f"{grand_diff:,.0f} ₸")
        st.divider()

        for result in results:
            valid = [r for r in result.records if not r.error or r.error == "Сумма не найдена"]
            bin_diff  = sum(rec.difference for rec in valid)
            err_count = sum(
                1 for rec in result.records
                if rec.error and rec.error != "Сумма не найдена"
            )
            with st.expander(
                f"БИН {result.bin} — {len(result.records)} договоров | разница: {bin_diff:,.0f} ₸",
                expanded=True,
            ):
                if err_count:
                    st.warning(f"⚠️ Ошибок при загрузке: {err_count}")
                    with st.expander("Подробности ошибок"):
                        for rec in result.records:
                            if rec.error and rec.error != "Сумма не найдена":
                                url_short = rec.url[-70:] if rec.url else "—"
                                st.caption(f"🔗 `{url_short}`  \n❌ {rec.error}")
                for rec in result.records[:10]:
                    has_error = rec.error and rec.error != "Сумма не найдена"
                    icon      = "⚠️" if has_error else "📄"
                    diff_str  = f"{rec.difference:,.0f} ₸" if not has_error else "—"
                    num_part  = f" №{rec.contract_number}" if rec.contract_number else ""
                    st.markdown(
                        f"{icon} **{rec.description[:80]}**{num_part}  \n"
                        f"Срок: `{rec.validity_period or '—'}` | "
                        f"Итог: `{rec.amount_final:,.0f} ₸` | "
                        f"Факт: `{rec.amount_actual:,.0f} ₸` | "
                        f"Разница: `{diff_str}`"
                        + (f"  — [{rec.url}]({rec.url})" if rec.url else "")
                    )
                if len(result.records) > 10:
                    st.caption(f"... и ещё {len(result.records) - 10} договоров в Excel-файле")

        st.divider()
        if st.session_state.excel_bytes:
            st.download_button(
                label="⬇️ Скачать Excel-отчёт",
                data=st.session_state.excel_bytes,
                file_name=get_report_filename(),
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                type="primary",
            )

    st.divider()
    if st.button("🔄 Новый анализ"):
        st.session_state.results         = None
        st.session_state.excel_bytes     = None
        st.session_state.bin_list        = [""]
        st.session_state.bins_to_process = []
        st.session_state.running         = False
        st.rerun()


# ── Подвал ────────────────────────────────────────────────────────────────
st.divider()
st.caption(
    f"Данные с портала [goszakup.gov.kz](https://goszakup.gov.kz/) · {datetime.now().day}-{datetime.now().month}-{datetime.now().year}"
    )
