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

from auth import (
    check_credentials,
    make_session_token,
    verify_session_token,
)
from excel_export import build_excel_report, get_report_filename
from excel_export_announcements import (
    build_excel_announcements_report,
    get_announcements_report_filename,
)
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
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Глобальные стили ──────────────────────────────────────────────────────

st.markdown("""
<style>
    /* Базовая палитра */
    :root {
        --ts-primary: #1a3a5c;
        --ts-primary-light: #2d5a87;
        --ts-accent: #3b82f6;
        --ts-bg-card: #ffffff;
        --ts-border: #e5e7eb;
        --ts-muted: #6b7280;
    }

    /* Скрываем стандартное меню/футер Streamlit */
    #MainMenu, footer {visibility: hidden;}

    /* Заголовки */
    .ts-app-title {
        font-size: 1.9rem;
        font-weight: 700;
        color: var(--ts-primary);
        margin-bottom: 0.2rem;
        letter-spacing: -0.02em;
    }
    .ts-subtitle {
        color: var(--ts-muted);
        font-size: 0.95rem;
        margin-bottom: 1.5rem;
    }
    .ts-page-title {
        font-size: 1.4rem;
        font-weight: 600;
        color: var(--ts-primary);
        margin: 0.5rem 0 0.25rem;
    }

    /* Кнопки */
    .stButton > button {
        border-radius: 8px;
        font-weight: 500;
    }
    div[data-testid="stDownloadButton"] button {
        background-color: var(--ts-primary);
        color: white;
        font-size: 1rem;
        padding: 0.6rem 1.5rem;
        border-radius: 8px;
        font-weight: 600;
    }

    /* Карточки на главной */
    .ts-card {
        background: var(--ts-bg-card);
        border: 1px solid var(--ts-border);
        border-radius: 14px;
        padding: 1.5rem 1.5rem 1.25rem;
        box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
        height: 100%;
    }
    .ts-card-icon {
        font-size: 2.2rem;
        margin-bottom: 0.6rem;
    }
    .ts-card-title {
        font-size: 1.15rem;
        font-weight: 600;
        color: var(--ts-primary);
        margin-bottom: 0.4rem;
    }
    .ts-card-text {
        color: #4b5563;
        font-size: 0.92rem;
        line-height: 1.45;
        margin-bottom: 1rem;
        min-height: 4.2em;
    }

    /* Сайдбар */
    section[data-testid="stSidebar"] {
        background: #f8fafc;
        border-right: 1px solid var(--ts-border);
    }
    .ts-user-card {
        background: white;
        border: 1px solid var(--ts-border);
        border-radius: 10px;
        padding: 0.85rem 0.95rem;
        margin-bottom: 0.6rem;
    }
    .ts-user-name {
        font-weight: 600;
        color: var(--ts-primary);
        font-size: 0.98rem;
    }
    .ts-user-role {
        color: var(--ts-muted);
        font-size: 0.82rem;
        margin-top: 0.15rem;
    }
    .ts-sidebar-section {
        font-size: 0.74rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        color: var(--ts-muted);
        margin: 0.4rem 0 0.4rem 0.25rem;
    }

    /* Окно логина по центру */
    .ts-login-wrap {
        max-width: 380px;
        margin: 3rem auto 0;
    }
</style>
""", unsafe_allow_html=True)


WORKER_PATH = Path(__file__).parent / "worker.py"
WORKER_TIMEOUT = 600  # секунд до принудительного таймаута

QUERY_PARAM_AUTH = "auth"

PAGE_HOME = "home"
PAGE_CONTRACTS = "contracts"
PAGE_ANNOUNCEMENTS = "announcements"


def form_container():
    """
    Узкая центральная колонка для форм ввода — на широком layout формы
    не растягиваются на всю ширину окна.
    Использовать как: `with form_container(): ...`.
    """
    _l, mid, _r = st.columns([1, 2, 1])
    return mid


def home_container():
    """Чуть шире, чем form_container — для карточек главной."""
    _l, mid, _r = st.columns([1, 3, 1])
    return mid


# ── Валидация ─────────────────────────────────────────────────────────────

def validate_bin(s: str) -> bool:
    return bool(re.fullmatch(r"\d{12}", s.strip()))


def _kill_worker_process(pid: int) -> None:
    """Убивает воркер и все его дочерние процессы (включая Chromium)."""
    try:
        if os.name == "nt":
            subprocess.call(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    except Exception as e:
        log.warning("Ошибка при остановке воркера PID=%s: %s", pid, e)


def _stop_worker() -> None:
    pid = st.session_state.get("worker_pid")
    if pid:
        _kill_worker_process(pid)
    st.session_state.running    = False
    st.session_state.worker_pid = None
    st.session_state.results    = None
    st.session_state.results_announcements = None


# ── Вспомогательные функции ───────────────────────────────────────────────

def _is_worker_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True


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
            specifics_2026_with_vat=r.get("specifics_2026_with_vat", 0.0),
            specifics_2026_without_vat=r.get("specifics_2026_without_vat", 0.0),
        )
        by_bin[b].records.append(cr)
        if cr.error:
            by_bin[b].errors.append(cr.error)
    return list(by_bin.values())


# ── Инициализация session_state ───────────────────────────────────────────

def _init_state():
    defaults = {
        "authenticated":         False,
        "current_user":          None,
        "page":                  PAGE_HOME,      # текущая страница в навигации
        "mode":                  None,            # "contracts" / "announcements" — для совместимости с воркером
        "selected_date":         None,
        "bin_list":               [""],
        "results":               None,
        "results_announcements": None,
        "excel_bytes":           None,
        "running":               False,
        "tmp_dir":               None,
        "progress_file":         None,
        "output_file":           None,
        "log_file_path":         None,
        "bins_to_process":       [],
        "worker_started_at":     0.0,
        "worker_pid":            None,
        "worker_error":          None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


_init_state()


# ── Восстановление сессии из URL-токена ───────────────────────────────────

def _restore_session_from_token() -> None:
    """Если в URL присутствует подписанный токен — пытаемся восстановить пользователя."""
    if st.session_state.authenticated:
        return
    try:
        token = st.query_params.get(QUERY_PARAM_AUTH)
    except Exception:
        token = None
    if not token:
        return
    user = verify_session_token(token)
    if user:
        st.session_state.authenticated = True
        st.session_state.current_user  = user
        log.info("Сессия восстановлена из токена: %s", user["username"])


def _persist_session_token(user: dict) -> None:
    """Записывает подписанный токен в URL-параметры, чтобы переживать refresh."""
    try:
        st.query_params[QUERY_PARAM_AUTH] = make_session_token(user)
    except Exception as e:
        log.warning("Не удалось записать auth-токен в query params: %s", e)


def _logout() -> None:
    pid = st.session_state.get("worker_pid")
    if pid:
        _kill_worker_process(pid)
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    try:
        if QUERY_PARAM_AUTH in st.query_params:
            del st.query_params[QUERY_PARAM_AUTH]
    except Exception:
        pass


_restore_session_from_token()


# ──────────────────────────────────────────────────────────────────────────
# СТРАНИЦА 0: Авторизация
# ──────────────────────────────────────────────────────────────────────────

if not st.session_state.authenticated:
    st.markdown('<div class="ts-app-title">TenderScope</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="ts-subtitle">Платформа для анализа реестра договоров '
        'и закупочных объявлений с портала goszakup.gov.kz</div>',
        unsafe_allow_html=True,
    )
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
                _persist_session_token(user)
                log.info("Вход: %s (%s)", user["username"], user["role"])
                st.rerun()
            else:
                st.error("Неверный логин или пароль")

    st.stop()


# ──────────────────────────────────────────────────────────────────────────
# Сайдбар: профиль + навигация + выход
# ──────────────────────────────────────────────────────────────────────────

user = st.session_state.current_user


def _nav_to(page: str) -> None:
    """Переход в навигации с корректным сбросом промежуточного состояния."""
    if st.session_state.running:
        # Прерываем активный воркер, чтобы не висел в фоне после ухода со страницы
        _stop_worker()

    st.session_state.page = page

    if page == PAGE_HOME:
        st.session_state.mode = None
        st.session_state.results = None
        st.session_state.results_announcements = None
        st.session_state.excel_bytes = None
        st.session_state.worker_error = None
        st.session_state.selected_date = None
        st.session_state.bin_list = [""]
        st.session_state.bins_to_process = []
    elif page == PAGE_CONTRACTS:
        st.session_state.mode = "contracts"
        st.session_state.results_announcements = None
    elif page == PAGE_ANNOUNCEMENTS:
        st.session_state.mode = "announcements"
        st.session_state.results = None


with st.sidebar:
    st.markdown('<div class="ts-app-title">TenderScope</div>', unsafe_allow_html=True)
    st.markdown(
        f"""
        <div class="ts-user-card">
            <div class="ts-user-name">👤 {user['display_name']}</div>
            <div class="ts-user-role">Роль: {user['role']}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown('<div class="ts-sidebar-section">Навигация</div>', unsafe_allow_html=True)

    current = st.session_state.page or PAGE_HOME

    nav_items = [
        (PAGE_HOME,          "🏠  Главная"),
        (PAGE_CONTRACTS,     "📋  Анализ договоров"),
        (PAGE_ANNOUNCEMENTS, "📢  Анализ объявлений"),
    ]
    for key, label in nav_items:
        btn_type = "primary" if current == key else "secondary"
        if st.button(label, key=f"nav_{key}", type=btn_type, use_container_width=True):
            _nav_to(key)
            st.rerun()

    st.divider()
    st.caption(f"📅 {datetime.now().strftime('%d.%m.%Y')}")

    if st.button("🚪  Выйти", key="nav_logout", use_container_width=True):
        _logout()
        st.rerun()


# ──────────────────────────────────────────────────────────────────────────
# Шапка
# ──────────────────────────────────────────────────────────────────────────

PAGE_TITLES = {
    PAGE_HOME:          ("Главная", "Выберите тип анализа для начала работы"),
    PAGE_CONTRACTS:     ("Анализ реестра договоров", "Парсинг договоров по введённым БИН поставщиков"),
    PAGE_ANNOUNCEMENTS: ("Анализ объявлений", "Парсинг объявлений с агрегированной информацией о победителях"),
}
title, subtitle = PAGE_TITLES.get(st.session_state.page or PAGE_HOME, ("TenderScope", ""))

st.markdown(f'<div class="ts-page-title">{title}</div>', unsafe_allow_html=True)
st.markdown(f'<div class="ts-subtitle">{subtitle}</div>', unsafe_allow_html=True)
st.divider()


# ──────────────────────────────────────────────────────────────────────────
# СТРАНИЦА «Главная»: две карточки выбора
# ──────────────────────────────────────────────────────────────────────────

if (st.session_state.page == PAGE_HOME
        and not st.session_state.running
        and st.session_state.results is None
        and st.session_state.results_announcements is None):

    with home_container():
        col1, col2 = st.columns(2, gap="large")

        with col1:
            st.markdown(
                """
                <div class="ts-card">
                    <div class="ts-card-icon">📋</div>
                    <div class="ts-card-title">Анализ реестра договоров</div>
                    <div class="ts-card-text">
                        Введите БИН компаний-поставщиков — система соберёт все действующие
                        договоры с goszakup.gov.kz, рассчитает разницу между итоговой
                        и фактической суммами и выгрузит Excel-отчёт.
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            if st.button("Перейти к анализу договоров",
                         key="goto_contracts",
                         type="primary",
                         use_container_width=True):
                _nav_to(PAGE_CONTRACTS)
                st.rerun()

        with col2:
            st.markdown(
                """
                <div class="ts-card">
                    <div class="ts-card-icon">📢</div>
                    <div class="ts-card-title">Анализ объявлений</div>
                    <div class="ts-card-text">
                        Выберите дату окончания приёма заявок — система найдёт
                        подходящие закупочные объявления, извлечёт информацию
                        о победителях и цене из протоколов.
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            if st.button("Перейти к анализу объявлений",
                         key="goto_announcements",
                         type="primary",
                         use_container_width=True):
                _nav_to(PAGE_ANNOUNCEMENTS)
                st.rerun()

    st.stop()


# ──────────────────────────────────────────────────────────────────────────
# СТРАНИЦА «Анализ объявлений»: выбор даты
# ──────────────────────────────────────────────────────────────────────────

if (st.session_state.page == PAGE_ANNOUNCEMENTS
        and st.session_state.results_announcements is None
        and not st.session_state.running):

    with form_container():
        st.markdown("#### Поиск объявлений по дате протокола итогов")

        col_from, col_to = st.columns(2)
        with col_from:
            date_from = st.date_input(
                "Протокол итогов с:",
                key="announcement_date_from",
            )
        with col_to:
            date_to = st.date_input(
                "Протокол итогов по:",
                key="announcement_date_to",
            )

        filter_bin = st.text_input(
            "БИН компании-победителя (необязательно):",
            key="announcement_filter_bin",
            placeholder="000000000000 (12 цифр) — оставьте пустым для всех",
            max_chars=12,
        ).strip()

        if filter_bin and not re.fullmatch(r"\d{12}", filter_bin):
            st.caption("⚠️ БИН должен содержать ровно 12 цифр")
            bin_valid = False
        else:
            bin_valid = True

        st.caption(
            "Применяются фиксированные фильтры: статус «Итоги опубликованы» и «Договор подписан», "
            "предмет закупки «Работа», сумма закупки от 1 500 000 000 ₸."
        )

        st.divider()

        dates_valid = date_from <= date_to
        if not dates_valid:
            st.warning("⚠️ Дата 'по' должна быть не раньше даты 'с'")

        run_announcements_clicked = st.button(
            "🔍 Запустить анализ",
            use_container_width=True, type="primary",
            key="run_announcements",
            disabled=not dates_valid or not bin_valid,
        )

    if run_announcements_clicked:
        date_str = date_from.strftime("%Y-%m-%d")
        date_to_str = date_to.strftime("%Y-%m-%d")
        st.session_state.selected_date = date_str

        tmp_dir = tempfile.mkdtemp(prefix="goszakup_announcements_")
        input_file = os.path.join(tmp_dir, "input.json")
        output_file = os.path.join(tmp_dir, "output.json")
        progress_file = os.path.join(tmp_dir, "progress.json")
        log_file_path = os.path.join(tmp_dir, "worker.log")

        with open(input_file, "w", encoding="utf-8") as f:
            json.dump({
                "mode": "announcements",
                "date": date_str,
                "date_to": date_to_str,
                "filter_bin": filter_bin if filter_bin else None,
                "progress_file": progress_file,
            }, f, ensure_ascii=False)

        log_file = open(log_file_path, "w", encoding="utf-8")
        popen_kwargs: dict = {"stdout": log_file, "stderr": sys.stderr}
        if os.name != "nt":
            popen_kwargs["preexec_fn"] = os.setsid

        proc = subprocess.Popen(
            [sys.executable, "-u", str(WORKER_PATH), input_file, output_file],
            **popen_kwargs,
        )
        log.info("Воркер запущен PID=%d  tmp=%s (объявления)", proc.pid, tmp_dir)

        st.session_state.running           = True
        st.session_state.mode              = "announcements"
        st.session_state.worker_pid        = proc.pid
        st.session_state.tmp_dir           = tmp_dir
        st.session_state.progress_file     = progress_file
        st.session_state.output_file       = output_file
        st.session_state.log_file_path     = log_file_path
        st.session_state.worker_started_at = time.time()
        st.session_state.worker_error      = None
        st.rerun()

    st.stop()


# ──────────────────────────────────────────────────────────────────────────
# СТРАНИЦА «Анализ договоров»: форма ввода БИН
# ──────────────────────────────────────────────────────────────────────────

if (st.session_state.page == PAGE_CONTRACTS
        and not st.session_state.running
        and st.session_state.results is None):

    with form_container():
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

        run_contracts_clicked = st.button(
            "🔍 Запустить анализ", disabled=not ok, type="primary",
            use_container_width=True, key="run_contracts",
        )

    if run_contracts_clicked:
        tmp_dir       = tempfile.mkdtemp(prefix="goszakup_")
        input_file    = os.path.join(tmp_dir, "input.json")
        output_file   = os.path.join(tmp_dir, "output.json")
        progress_file = os.path.join(tmp_dir, "progress.json")
        log_file_path = os.path.join(tmp_dir, "worker.log")

        with open(input_file, "w", encoding="utf-8") as f:
            json.dump({"bins": filled, "progress_file": progress_file}, f, ensure_ascii=False)

        log_file = open(log_file_path, "w", encoding="utf-8")
        popen_kwargs: dict = {"stdout": log_file, "stderr": sys.stderr}
        if os.name != "nt":
            popen_kwargs["preexec_fn"] = os.setsid

        proc = subprocess.Popen(
            [sys.executable, "-u", str(WORKER_PATH), input_file, output_file],
            **popen_kwargs,
        )
        log.info("Воркер запущен PID=%d  tmp=%s", proc.pid, tmp_dir)

        st.session_state.running           = True
        st.session_state.mode              = "contracts"
        st.session_state.worker_pid        = proc.pid
        st.session_state.tmp_dir           = tmp_dir
        st.session_state.progress_file     = progress_file
        st.session_state.output_file       = output_file
        st.session_state.log_file_path     = log_file_path
        st.session_state.bins_to_process   = filled
        st.session_state.results           = None
        st.session_state.excel_bytes       = None
        st.session_state.worker_started_at = time.time()
        st.session_state.worker_error      = None
        st.rerun()


# ──────────────────────────────────────────────────────────────────────────
# СТРАНИЦА «Прогресс»: file-based polling
# ──────────────────────────────────────────────────────────────────────────

if st.session_state.running:
    progress_file = st.session_state.progress_file
    output_file   = st.session_state.output_file
    started_at    = st.session_state.worker_started_at

    if st.session_state.mode == "announcements":
        st.markdown("#### 📡 Идёт сбор данных объявлений...")
    else:
        st.markdown("#### 📡 Идёт сбор данных договоров...")

    prog = _read_json(progress_file) if os.path.exists(progress_file) else {}
    done = prog.get("done", False)
    msg = prog.get("message", "Запуск браузера Chromium...")

    if st.session_state.mode == "announcements":
        a_cur = prog.get("announcement_current", 0)
        a_tot = prog.get("announcement_total", 0)
        if a_tot > 0:
            st.markdown(f"Объявление **{a_cur}** из **{a_tot}**")
        st.progress(a_cur / max(a_tot, 1), text=f"Прогресс: {a_cur}/{a_tot}")
    else:
        b_name = prog.get("bin_name", "")
        b_cur = prog.get("bin_current", 0)
        b_tot = prog.get("bin_total", len(st.session_state.bins_to_process))
        c_cur = prog.get("contract_current", 0)
        c_tot = prog.get("contract_total", 0)

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

    output_ready = os.path.exists(output_file)

    if done and output_ready:
        try:
            data = _read_json(output_file)
            error = data.get("error")
            all_recs = data.get("records", [])

            if st.session_state.mode == "announcements":
                from scraper_announcements import AnnouncementRecord, LotRecord, ScrapeAnnouncementsResult

                def _deserialize_announcement(r: dict) -> AnnouncementRecord:
                    lots = [
                        LotRecord(
                            lot_number=lot.get("lot_number", ""),
                            lot_name=lot.get("lot_name", ""),
                            lot_amount=lot.get("lot_amount", 0.0),
                            winner_name=lot.get("winner_name", ""),
                            winner_bin=lot.get("winner_bin", ""),
                            winner_price=lot.get("winner_price", 0.0),
                            year1_sum=lot.get("year1_sum", 0.0),
                        )
                        for lot in r.get("lots", [])
                    ]
                    return AnnouncementRecord(
                        number=r.get("number", 0),
                        name=r.get("name", ""),
                        method=r.get("method", ""),
                        start_date=r.get("start_date", ""),
                        end_date=r.get("end_date", ""),
                        sum_amount=r.get("sum_amount", 0.0),
                        status=r.get("status", ""),
                        winner_name=r.get("winner_name", ""),
                        winner_bin=r.get("winner_bin", ""),
                        winner_price=r.get("winner_price", 0.0),
                        url=r.get("url", ""),
                        has_contracts=r.get("has_contracts", False),
                        error=r.get("error", ""),
                        lots=lots,
                    )

                results = ScrapeAnnouncementsResult(
                    selected_date=st.session_state.selected_date,
                    records=[_deserialize_announcement(r) for r in all_recs],
                )
                log.info("Загружено %d объявлений", len(all_recs))

                excel_bytes = None
                if results.records:
                    try:
                        excel_bytes = build_excel_announcements_report(results)
                    except Exception as exc:
                        log.exception("Ошибка Excel (объявления): %s", exc)

                st.session_state.worker_error = error
                st.session_state.results_announcements = results
            else:
                results = _records_to_results(all_recs)
                total_loaded = sum(len(r.records) for r in results)
                log.info("Загружено %d записей", total_loaded)

                err_recs = [r for r in all_recs
                            if r.get("error") and r.get("error") != "Сумма не найдена"]
                if err_recs:
                    log.warning("Ошибок при парсинге: %d из %d", len(err_recs), total_loaded)
                    for r in err_recs[:30]:
                        log.warning("  БИН %s | %s | %s",
                                    r.get("bin", "?"),
                                    r.get("url", "")[-70:],
                                    r.get("error", "?"))

                excel_bytes = None
                if results:
                    try:
                        excel_bytes = build_excel_report(results)
                    except Exception as exc:
                        log.exception("Ошибка Excel: %s", exc)

                st.session_state.worker_error = error
                st.session_state.results = results

            st.session_state.excel_bytes = excel_bytes
            st.session_state.running = False

        except Exception as exc:
            error = f"Ошибка чтения результата: {exc}"
            log.exception(error)
            st.session_state.worker_error = error
            st.session_state.running = False

        st.rerun()

    elif done and not output_ready:
        time.sleep(0.5)
        st.rerun()

    elif not _is_worker_alive(st.session_state.get("worker_pid")):
        partial: list[dict] = []
        if os.path.exists(output_file):
            try:
                partial = _read_json(output_file).get("records", [])
            except Exception:
                pass

        if partial:
            log.warning("Воркер завершился досрочно. Частичных записей: %d", len(partial))

            if st.session_state.mode == "announcements":
                from scraper_announcements import AnnouncementRecord, LotRecord, ScrapeAnnouncementsResult

                def _deserialize_announcement_partial(r: dict) -> AnnouncementRecord:
                    lots = [
                        LotRecord(
                            lot_number=lot.get("lot_number", ""),
                            lot_name=lot.get("lot_name", ""),
                            lot_amount=lot.get("lot_amount", 0.0),
                            winner_name=lot.get("winner_name", ""),
                            winner_bin=lot.get("winner_bin", ""),
                            winner_price=lot.get("winner_price", 0.0),
                            year1_sum=lot.get("year1_sum", 0.0),
                        )
                        for lot in r.get("lots", [])
                    ]
                    return AnnouncementRecord(
                        number=r.get("number", 0),
                        name=r.get("name", ""),
                        method=r.get("method", ""),
                        start_date=r.get("start_date", ""),
                        end_date=r.get("end_date", ""),
                        sum_amount=r.get("sum_amount", 0.0),
                        status=r.get("status", ""),
                        winner_name=r.get("winner_name", ""),
                        winner_bin=r.get("winner_bin", ""),
                        winner_price=r.get("winner_price", 0.0),
                        url=r.get("url", ""),
                        has_contracts=r.get("has_contracts", False),
                        error=r.get("error", ""),
                        lots=lots,
                    )

                results = ScrapeAnnouncementsResult(
                    selected_date=st.session_state.selected_date,
                    records=[_deserialize_announcement_partial(r) for r in partial],
                )
                excel_bytes = None
                try:
                    excel_bytes = build_excel_announcements_report(results)
                except Exception as exc:
                    log.exception("Ошибка Excel (объявления): %s", exc)

                st.session_state.results_announcements = results
                st.session_state.worker_error = (
                    f"⚠️ Парсинг прерван досрочно — сервер нагружен. "
                    f"Сохранено {len(partial)} объявлений."
                )
            else:
                results = _records_to_results(partial)
                excel_bytes = None
                try:
                    excel_bytes = build_excel_report(results)
                except Exception as exc:
                    log.exception("Ошибка Excel (частичные): %s", exc)

                st.session_state.results = results
                c_tot = prog.get("contract_total", 0)
                st.session_state.worker_error = (
                    f"⚠️ Парсинг прерван досрочно — сервер нагружен. "
                    f"Сохранено {len(partial)} из ~{c_tot} договоров."
                )

            st.session_state.excel_bytes = excel_bytes
        else:
            log.error("Воркер завершился без данных")
            if st.session_state.mode == "announcements":
                st.session_state.results_announcements = []
            else:
                st.session_state.results = []
            st.session_state.worker_error = "Воркер завершился неожиданно без данных."

        st.session_state.running = False
        st.rerun()

    elif time.time() - started_at > WORKER_TIMEOUT:
        st.error(f"❌ Превышено время ожидания ({WORKER_TIMEOUT // 60} мин). Проверьте интернет-соединение.")
        st.session_state.running = False
        st.rerun()

    else:
        time.sleep(1)
        st.rerun()


# ──────────────────────────────────────────────────────────────────────────
# СТРАНИЦА «Результаты — Договоры»
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
        worker_err = st.session_state.get("worker_error")
        if worker_err and worker_err.startswith("⚠️"):
            st.warning(worker_err)
        elif worker_err:
            st.error(f"❌ {worker_err}")

        if not worker_err:
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
    if st.button("🔄 Новый анализ", key="new_analysis_contracts"):
        _nav_to(PAGE_HOME)
        st.rerun()


# ──────────────────────────────────────────────────────────────────────────
# СТРАНИЦА «Результаты — Объявления»
# ──────────────────────────────────────────────────────────────────────────

if st.session_state.results_announcements is not None and not st.session_state.running:
    from scraper_announcements import ScrapeAnnouncementsResult

    results: ScrapeAnnouncementsResult = st.session_state.results_announcements

    if not results.records:
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
        worker_err = st.session_state.get("worker_error")
        if worker_err and worker_err.startswith("⚠️"):
            st.warning(worker_err)
        elif worker_err:
            st.error(f"❌ {worker_err}")

        if not worker_err:
            st.success("✅ Анализ объявлений успешно завершён!")
        st.markdown("#### 📊 Результаты объявлений")

        total_announcements = len(results.records)
        total_errors = len(results.errors)
        total_sum = sum(r.sum_amount for r in results.records if r.sum_amount > 0)
        total_price = sum(r.winner_price for r in results.records if r.winner_price > 0)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Всего объявлений", total_announcements)
        c2.metric("Ошибок при сборе", total_errors)
        c3.metric("Сумма закупок",   f"{total_sum:,.0f} ₸")
        c4.metric("Цена победителей",f"{total_price:,.0f} ₸")
        st.divider()

        for rec in results.records[:15]:
            has_error = bool(rec.error)
            icon = "⚠️" if has_error else "📢"
            winner_info = (
                f"{rec.winner_name} (БИН: {rec.winner_bin})"
                if rec.winner_bin else "—"
            )
            if rec.has_contracts:
                price_str = "(есть договоры — цена пуста)"
            elif rec.winner_price > 0:
                price_str = f"{rec.winner_price:,.0f} ₸"
            else:
                price_str = "—"

            st.markdown(
                f"{icon} **№{rec.number}. {rec.name[:70] or '(без названия)'}**  \n"
                f"Способ: `{rec.method or '—'}` | Статус: `{rec.status or '—'}`  \n"
                f"Сумма: `{rec.sum_amount:,.0f} ₸` | Победитель: `{winner_info}` | Цена: `{price_str}`  \n"
                f"Даты: `{rec.start_date}` — `{rec.end_date}`"
                + (f"  — [{rec.url}]({rec.url})" if rec.url else "")
            )
            st.divider()

        if len(results.records) > 15:
            st.caption(f"... и ещё {len(results.records) - 15} объявлений в Excel-файле")

        st.divider()
        if st.session_state.excel_bytes:
            st.download_button(
                label="⬇️ Скачать Excel-отчёт",
                data=st.session_state.excel_bytes,
                file_name=get_announcements_report_filename(),
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                type="primary",
            )

    st.divider()
    if st.button("🔄 Новый анализ", key="new_analysis_announcements"):
        _nav_to(PAGE_HOME)
        st.rerun()


# ── Подвал ────────────────────────────────────────────────────────────────
st.divider()
st.caption(
    f"Данные с портала [goszakup.gov.kz](https://goszakup.gov.kz/) · "
    f"{datetime.now().strftime('%d.%m.%Y')}"
)