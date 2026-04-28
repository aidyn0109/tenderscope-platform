import hashlib
import os
import streamlit as st


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