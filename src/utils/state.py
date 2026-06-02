import json
import os
import time
from typing import Any, Dict

DEFAULT_STATE_FILE = "/var/lib/vps-ip-bot/state.json"


def _state_file() -> str:
    env_path = os.getenv("VPS_IP_BOT_STATE_FILE")
    if env_path:
        return os.path.abspath(os.path.expanduser(env_path))

    try:
        from config import config

        configured = str(config.get("state_file") or DEFAULT_STATE_FILE).strip()
    except Exception:
        configured = DEFAULT_STATE_FILE

    return os.path.abspath(os.path.expanduser(configured))


def _default_state() -> Dict[str, Any]:
    return {
        "last_change_time": 0,
        "last_old_ip": "",
        "last_new_ip": "",
        "last_change_status": "",
        "last_change_trigger": "",
        "last_dns_result": "",
        "last_message": "",
        "last_success": False,
        "last_chat_id": "",
        "pending_notify": False,
        "sending_notify": False,
        "notified_at": 0,
        "updated_at": 0,
    }


def _ensure_parent() -> None:
    parent = os.path.dirname(_state_file())
    if parent:
        os.makedirs(parent, exist_ok=True)


def load_state() -> Dict[str, Any]:
    state_file = _state_file()
    if not os.path.exists(state_file):
        return _default_state()
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        state = _default_state()
        state.update(data)
        return state
    except Exception:
        return _default_state()


def save_state(data: Dict[str, Any]) -> None:
    _ensure_parent()
    with open(_state_file(), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_last_change_time() -> float:
    return float(load_state().get("last_change_time", 0) or 0)


def update_change_state(
    *,
    old_ip: str = "",
    new_ip: str = "",
    status: str = "",
    trigger: str = "",
    dns_result: str = "",
    message: str = "",
    success: bool = False,
    chat_id: str = "",
    pending_notify: bool = False,
) -> None:
    state = load_state()
    state["last_change_time"] = time.time()
    state["last_old_ip"] = old_ip
    state["last_new_ip"] = new_ip
    state["last_change_status"] = status
    state["last_change_trigger"] = trigger
    state["last_dns_result"] = dns_result
    state["last_message"] = message
    state["last_success"] = success
    state["last_chat_id"] = str(chat_id or "")
    state["pending_notify"] = pending_notify
    state["sending_notify"] = False
    state["updated_at"] = time.time()
    if not pending_notify:
        state["notified_at"] = time.time()
    save_state(state)


def get_pending_notification() -> Dict[str, Any]:
    state = load_state()
    if state.get("pending_notify") and state.get("last_message"):
        return state
    return {}


def mark_sending_notify(value: bool) -> None:
    state = load_state()
    state["sending_notify"] = bool(value)
    state["updated_at"] = time.time()
    save_state(state)


def mark_notification_sent() -> None:
    state = load_state()
    state["pending_notify"] = False
    state["sending_notify"] = False
    state["notified_at"] = time.time()
    state["updated_at"] = time.time()
    save_state(state)
