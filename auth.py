"""
auth.py — Авторизация пользователей через st.secrets
"""

import hashlib
import streamlit as st


def _get_users() -> dict:
    """Читает пользователей из st.secrets (Render) или secrets.toml (локально)."""
    try:
        users = {}
        for username, data in st.secrets["users"].items():
            users[username] = {
                "password_hash": data["password_hash"],
                "role":          data["role"],
                "display_name":  data["display_name"],
            }
        return users
    except Exception:
        return {}


def check_credentials(username: str, password: str) -> dict | None:
    """Возвращает данные пользователя если логин/пароль верны, иначе None."""
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