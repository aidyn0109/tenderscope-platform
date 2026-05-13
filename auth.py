import base64
import hashlib
import hmac
import json
import os
import time

import streamlit as st

# ── Конфигурация подписанных сессионных токенов ────────────────────────────

# Секрет для подписи токенов. ВАЖНО: на продакшене должен быть задан через
# переменную окружения AUTH_SECRET, иначе подписи можно будет подделать,
# зная исходный код.
_DEFAULT_SECRET = "tenderscope-default-secret-please-override-with-env-AUTH_SECRET"
SESSION_TTL_SECONDS = 7 * 24 * 3600  # 7 дней


def _secret() -> bytes:
    try:
        s = st.secrets.get("auth_secret")  # type: ignore[attr-defined]
        if s:
            return str(s).encode()
    except Exception:
        pass
    return os.environ.get("AUTH_SECRET", _DEFAULT_SECRET).encode()


def make_session_token(user: dict, ttl: int = SESSION_TTL_SECONDS) -> str:
    """
    Делает компактный самодостаточный токен `<payload>.<sig>`, где
    payload — base64url(JSON), sig — первые 16 байт HMAC-SHA256 от payload.
    Не требует серверного хранения сессии.
    """
    payload = {
        "u": user["username"],
        "r": user["role"],
        "d": user["display_name"],
        "e": int(time.time()) + int(ttl),
    }
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    ).decode().rstrip("=")
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{sig}"


def verify_session_token(token: str) -> dict | None:
    """Возвращает данные пользователя, если токен валидный и не истёк, иначе None."""
    if not token or "." not in token:
        return None
    try:
        body, sig = token.rsplit(".", 1)
        expected = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, expected):
            return None
        padded = body + "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        if int(payload.get("e", 0)) < int(time.time()):
            return None
        return {
            "username":     payload["u"],
            "role":         payload["r"],
            "display_name": payload["d"],
        }
    except Exception:
        return None


def _get_users() -> dict:
    # Сначала пробуем st.secrets
    try:
        users = {}
        for username, data in st.secrets["users"].items():
            users[username] = {
                "password_hash": data["password_hash"],
                "role":          data["role"],
                "display_name":  data["display_name"],
            }
        if users:
            return users
    except Exception:
        pass

    # Читаем из переменных окружения на Render
    users = {}
    admin_hash = os.environ.get("ADMIN_HASH")
    user_hash  = os.environ.get("USER_HASH")

    if admin_hash:
        users["admin"] = {
            "password_hash": admin_hash,
            "role":          os.environ.get("ADMIN_ROLE", "admin"),
            "display_name":  os.environ.get("ADMIN_DISPLAY", "Администратор"),
        }
    if user_hash:
        users["user"] = {
            "password_hash": user_hash,
            "role":          os.environ.get("USER_ROLE", "user"),
            "display_name":  os.environ.get("USER_DISPLAY", "Пользователь"),
        }
    return users


def check_credentials(username: str, password: str) -> dict | None:
    users = _get_users()
    user  = users.get(username.strip().lower())
    if not user:
        return None
    if user["password_hash"] == hashlib.sha256(password.encode()).hexdigest():
        return {
            "username":     username,
            "role":         user["role"],
            "display_name": user["display_name"],
        }
    return None