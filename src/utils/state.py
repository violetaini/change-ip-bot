import json
import os
import time
from typing import Any, Dict, Optional

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


def _default_server_state() -> Dict[str, Any]:
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
        "boil_next_allowed_at": 0,
        "boil_uses_left": -1,
    }


def _default_state() -> Dict[str, Any]:
    base = _default_server_state()
    base["servers"] = {}
    base["user_active_servers"] = {}
    return base


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
        if "servers" not in state or not isinstance(state["servers"], dict):
            state["servers"] = {}
        if "default" not in state["servers"]:
            default_entry = _default_server_state()
            for k in default_entry:
                if k in state:
                    default_entry[k] = state[k]
            state["servers"]["default"] = default_entry
        if "user_active_servers" not in state or not isinstance(state["user_active_servers"], dict):
            state["user_active_servers"] = {}
        return state
    except Exception:
        return _default_state()


def save_state(data: Dict[str, Any]) -> None:
    _ensure_parent()
    target = _state_file()
    temp_target = f"{target}.tmp.{os.getpid()}"
    with open(temp_target, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(temp_target, target)


def load_server_state(server_id: str = "default") -> Dict[str, Any]:
    state = load_state()
    sid = str(server_id or "default").strip()
    servers = state.get("servers", {})
    if sid in servers:
        s_data = _default_server_state()
        s_data.update(servers[sid])
        return s_data
    if sid == "default":
        s_data = _default_server_state()
        for k in s_data:
            if k in state:
                s_data[k] = state[k]
        return s_data
    return _default_server_state()


def save_server_state(server_id: str, server_data: Dict[str, Any]) -> None:
    sid = str(server_id or "default").strip()
    state = load_state()
    if "servers" not in state or not isinstance(state["servers"], dict):
        state["servers"] = {}
    state["servers"][sid] = server_data
    if sid == "default":
        for k, v in server_data.items():
            state[k] = v
    state["updated_at"] = time.time()
    save_state(state)


def update_server_state_keys(server_id: str, updates: Dict[str, Any]) -> None:
    s_data = load_server_state(server_id)
    s_data.update(updates)
    s_data["updated_at"] = time.time()
    save_server_state(server_id, s_data)


def update_state_keys(updates: Dict[str, Any], server_id: str = "default") -> None:
    update_server_state_keys(server_id, updates)


def get_last_change_time(server_id: str = "default") -> float:
    return float(load_server_state(server_id).get("last_change_time", 0) or 0)


def update_server_change_state(
    server_id: str = "default",
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
    s_data = load_server_state(server_id)
    now = time.time()
    s_data["last_change_time"] = now
    s_data["last_old_ip"] = old_ip
    s_data["last_new_ip"] = new_ip
    s_data["last_change_status"] = status
    s_data["last_change_trigger"] = trigger
    s_data["last_dns_result"] = dns_result
    s_data["last_message"] = message
    s_data["last_success"] = success
    s_data["last_chat_id"] = str(chat_id or "")
    s_data["pending_notify"] = pending_notify
    s_data["sending_notify"] = False
    s_data["updated_at"] = now
    if not pending_notify:
        s_data["notified_at"] = now
    save_server_state(server_id, s_data)


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
    server_id: str = "default",
) -> None:
    update_server_change_state(
        server_id=server_id,
        old_ip=old_ip,
        new_ip=new_ip,
        status=status,
        trigger=trigger,
        dns_result=dns_result,
        message=message,
        success=success,
        chat_id=chat_id,
        pending_notify=pending_notify,
    )


def get_pending_notification(server_id: str = "default") -> Dict[str, Any]:
    s_data = load_server_state(server_id)
    if s_data.get("pending_notify") and s_data.get("last_message"):
        return s_data
    return {}


def get_all_pending_notifications() -> list[tuple[str, Dict[str, Any]]]:
    state = load_state()
    res = []
    servers = state.get("servers", {})
    for s_id, s_data in servers.items():
        if isinstance(s_data, dict) and s_data.get("pending_notify") and s_data.get("last_message"):
            res.append((s_id, s_data))
    return res


def mark_sending_notify(value: bool, server_id: str = "default") -> None:
    update_server_state_keys(server_id, {"sending_notify": bool(value)})


def mark_notification_sent(server_id: str = "default") -> None:
    now = time.time()
    update_server_state_keys(server_id, {
        "pending_notify": False,
        "sending_notify": False,
        "notified_at": now,
        "updated_at": now,
    })


def get_user_selected_server(user_id: int | str) -> Optional[str]:
    state = load_state()
    return state.get("user_active_servers", {}).get(str(user_id))


def set_user_selected_server(user_id: int | str, server_id: str) -> None:
    state = load_state()
    if "user_active_servers" not in state or not isinstance(state["user_active_servers"], dict):
        state["user_active_servers"] = {}
    state["user_active_servers"][str(user_id)] = str(server_id).strip()
    save_state(state)
