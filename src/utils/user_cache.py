import os
import re
import threading
import time
from typing import Any, Dict, Optional, Tuple

from utils.logger import logger
from utils.state import load_state, save_state

_CACHE_LOCK = threading.RLock()
_USERS_CACHE: Dict[str, Dict[str, Any]] = {}
_USERNAME_INDEX: Dict[str, str] = {}
_CACHE_INITIALIZED = False


def _ensure_cache_loaded() -> None:
    global _CACHE_INITIALIZED, _USERS_CACHE, _USERNAME_INDEX
    with _CACHE_LOCK:
        if not _CACHE_INITIALIZED:
            try:
                state = load_state()
                raw_users = state.get("known_users", {})
                if isinstance(raw_users, dict):
                    _USERS_CACHE = {
                        str(k): dict(v)
                        for k, v in raw_users.items()
                        if isinstance(v, dict)
                    }
                    _USERNAME_INDEX = {}
                    for uid, info in _USERS_CACHE.items():
                        u = str(info.get("u") or "").strip().lower()
                        if u:
                            _USERNAME_INDEX[u] = uid
                else:
                    _USERS_CACHE = {}
                    _USERNAME_INDEX = {}
            except Exception as e:
                logger.warning(f"加载 known_users 失败: {e}")
                _USERS_CACHE = {}
                _USERNAME_INDEX = {}
            _CACHE_INITIALIZED = True


def _reset_cache_for_testing() -> None:
    global _CACHE_INITIALIZED, _USERS_CACHE, _USERNAME_INDEX
    with _CACHE_LOCK:
        _CACHE_INITIALIZED = False
        _USERS_CACHE = {}
        _USERNAME_INDEX = {}


def clean_username(raw: str) -> str:
    """
    清洗并规范化 Telegram 用户名。
    支持输入格式：
      - @username
      - t.me/username
      - https://t.me/username
      - http://t.me/username
      - tg://resolve?domain=username
      - telegram.me/username
      - 纯用户名字符串
    返回小写用户名（不含 @），若不符合 Telegram 用户名格式则返回空字符串。
    Telegram 用户名标准：3-32 位英文字母、数字、下划线。
    """
    if not raw:
        return ""
    s = str(raw).strip()

    # 移除 URL 协议和域名
    s = re.sub(r"^(?:https?:\/\/)?(?:www\.)?(?:t\.me|telegram\.me)\/", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^tg:\/\/resolve\?domain=", "", s, flags=re.IGNORECASE)

    # 截断查询参数或锚点
    s = s.split("?")[0].split("#")[0]

    # 移除首尾斜杠及 @
    s = s.strip("/").lstrip("@").strip()

    # 验证 Telegram 用户名格式：3-32位字母数字下划线
    if re.fullmatch(r"[a-zA-Z0-9_]{3,32}", s):
        return s.lower()
    return ""


def record_known_user(user_id: int | str, username: str = "", full_name: str = "") -> None:
    """
    记录或更新已知的 Telegram 用户映射（user_id <-> username）。
    采用内存索引 + state.json 轻量持久化，支持写入节流与容量上限控制。
    """
    uid = str(user_id or "").strip()
    if not uid or not uid.isdigit():
        return

    _ensure_cache_loaded()

    clean_u = clean_username(username)
    clean_n = str(full_name or "").strip()[:64]
    now = int(time.time())

    with _CACHE_LOCK:
        existing = _USERS_CACHE.get(uid)
        if existing:
            old_u = str(existing.get("u") or "")
            old_n = str(existing.get("n") or "")
            old_t = int(existing.get("t") or 0)
            # 节流机制：用户名和昵称未变更且12小时内更新过，无需重复写入磁盘
            if old_u == clean_u and old_n == clean_n and (now - old_t < 43200):
                return
            # 若旧用户名已变更，移除旧用户名映射
            if old_u and old_u in _USERNAME_INDEX and _USERNAME_INDEX[old_u] == uid:
                del _USERNAME_INDEX[old_u]

        _USERS_CACHE[uid] = {
            "u": clean_u,
            "n": clean_n,
            "t": now,
        }
        if clean_u:
            _USERNAME_INDEX[clean_u] = uid

        # 轻量容量上限控制：超过 5000 条记录时，清理最早的 1000 条活跃记录
        if len(_USERS_CACHE) > 5000:
            sorted_items = sorted(_USERS_CACHE.items(), key=lambda kv: kv[1].get("t", 0))
            for old_uid, old_info in sorted_items[:1000]:
                del _USERS_CACHE[old_uid]
                u_to_del = str(old_info.get("u") or "")
                if u_to_del and _USERNAME_INDEX.get(u_to_del) == old_uid:
                    del _USERNAME_INDEX[u_to_del]

        # 异步持久化到 state.json
        try:
            state = load_state()
            state["known_users"] = _USERS_CACHE
            save_state(state)
        except Exception as e:
            logger.warning(f"保存 known_users 失败: {e}")


def get_known_user(user_id: int | str) -> Optional[Dict[str, Any]]:
    """获取指定 user_id 的缓存用户资料"""
    uid = str(user_id or "").strip()
    if not uid:
        return None
    _ensure_cache_loaded()
    with _CACHE_LOCK:
        info = _USERS_CACHE.get(uid)
        return dict(info) if info else None


def find_user_id_by_username(username: str) -> Optional[str]:
    """通过 Telegram @username 查找对应的纯数字 User ID"""
    clean_u = clean_username(username)
    if not clean_u:
        return None
    _ensure_cache_loaded()
    with _CACHE_LOCK:
        return _USERNAME_INDEX.get(clean_u)


def extract_user_from_forward(message: Any) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    从转发消息中提取目标用户的 Telegram User ID 与 Username。
    返回: (user_id, username, error_message)
    若不是转发消息，返回 (None, None, None)
    """
    if not message:
        return None, None, None

    # Telegram Bot API 7.0+ (python-telegram-bot v21+) MessageOrigin
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        orig_type = str(getattr(origin, "type", "")).lower()
        if orig_type == "user" and hasattr(origin, "sender_user"):
            sender = getattr(origin, "sender_user", None)
            if sender and getattr(sender, "id", None):
                sid = str(sender.id)
                uname = clean_username(getattr(sender, "username", "") or "")
                fname = getattr(sender, "full_name", "") or getattr(sender, "first_name", "") or ""
                record_known_user(sid, uname, fname)
                return sid, uname, None
        elif orig_type == "hidden_user":
            return (
                None,
                None,
                "该用户开启了转发隐私保护（隐藏账号来源），无法自动提取 User ID。请让该用户先向本机器人发送一次 /start，或直接输入其纯数字 User ID。",
            )
        elif orig_type in ("channel", "chat"):
            return None, None, "检测到转发自频道或群组，无法提取个人用户 ID。"

    # Legacy: message.forward_from (User)
    fw_user = getattr(message, "forward_from", None)
    if fw_user is not None and getattr(fw_user, "id", None):
        sid = str(fw_user.id)
        uname = clean_username(getattr(fw_user, "username", "") or "")
        fname = getattr(fw_user, "full_name", "") or getattr(fw_user, "first_name", "") or ""
        record_known_user(sid, uname, fname)
        return sid, uname, None

    # Legacy: message.forward_sender_name (hidden user)
    if getattr(message, "forward_sender_name", None):
        return (
            None,
            None,
            "该用户开启了转发隐私保护（隐藏账号来源），无法自动提取 User ID。请让该用户先向本机器人发送一次 /start，或直接输入其纯数字 User ID。",
        )

    return None, None, None


def parse_and_resolve_user_id(raw_input: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    解析用户输入的管理员标识符。
    支持输入格式：
      1. 纯数字 User ID（5-20位），例如：123456789
      2. 用户名，例如：@alice、alice
      3. Telegram 用户链接，例如：t.me/alice、https://t.me/alice
    返回: (resolved_user_id, username, error_message)
    """
    if not raw_input:
        return None, None, "输入内容不能为空。"
    s = str(raw_input).strip()

    # 1. 纯数字 User ID (5-20 位)
    if re.fullmatch(r"\d{5,20}", s):
        user_info = get_known_user(s)
        uname = user_info.get("u", "") if user_info else ""
        return s, uname, None

    # 2. 用户名或 t.me 链接
    cleaned_u = clean_username(s)
    if cleaned_u:
        matched_uid = find_user_id_by_username(cleaned_u)
        if matched_uid:
            return matched_uid, cleaned_u, None
        return (
            None,
            cleaned_u,
            (
                f"未在机器人本地记录中找到用户 @{cleaned_u}。\n\n"
                f"💡 原因：受 Telegram 隐私保护限制，机器人无法直接向 Telegram 查询未互动过的陌生人。\n\n"
                f"👉 解决方法：\n"
                f"1. 请让该用户先向本机器人发送一次 /start；\n"
                f"2. 或直接在此输入该用户的纯数字 Telegram User ID。"
            ),
        )

    # 3. 既非有效数字 ID，也非有效用户名
    return (
        None,
        None,
        (
            "输入格式无效。支持输入：\n"
            "• 纯数字 User ID（如 `123456789`）\n"
            "• 用户名（如 `@username` 或 `t.me/username`）\n"
            "• 或直接转发该用户发送给您的任意消息"
        ),
    )


def format_user_with_id(user_id: int | str) -> str:
    """格式化展示用户 ID 及其已知的 @username"""
    uid = str(user_id or "").strip()
    if not uid:
        return ""
    info = get_known_user(uid)
    if info and info.get("u"):
        return f"`{uid}` (@{info['u']})"
    return f"`{uid}`"


def format_user_button_label(user_id: int | str, prefix: str = "") -> str:
    """格式化按钮上的标签文本"""
    uid = str(user_id or "").strip()
    info = get_known_user(uid)
    p = f"{prefix} " if prefix else ""
    if info and info.get("u"):
        return f"{p}{uid} (@{info['u']})"
    return f"{p}{uid}"
