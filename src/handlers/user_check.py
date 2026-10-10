from config import config
from utils.logger import logger


def _split_config_ids(value):
    return {x.strip() for x in str(value or "").split(",") if x.strip()}


def _roles_configured():
    return bool(_split_config_ids(config.get("telegram_super_admin_user_ids"))) or bool(
        _split_config_ids(config.get("telegram_admin_user_ids"))
    )


def _legacy_allowed(update):
    user = update.effective_user
    chat = update.effective_chat
    user_id = str(user.id) if user else ""
    chat_id = str(chat.id) if chat else ""

    configured_users = _split_config_ids(config.get("telegram_allowed_user_ids"))
    configured_chats = _split_config_ids(config.get("telegram_chat_id"))

    if configured_users:
        return user_id in configured_users
    return user_id in configured_chats or chat_id in configured_chats


def _record_user_if_present(user):
    if user and getattr(user, "id", None):
        try:
            from utils.user_cache import record_known_user
            record_known_user(
                user.id,
                getattr(user, "username", "") or "",
                getattr(user, "full_name", "") or getattr(user, "first_name", "") or "",
            )
        except Exception:
            pass


def get_user_role(update):
    user = update.effective_user if update else None
    _record_user_if_present(user)
    user_id = str(user.id) if user else ""
    if not user_id:
        return ""

    super_admins = _split_config_ids(config.get("telegram_super_admin_user_ids"))
    # 普通管理员列表必须严格排除超级管理员，防止角色混叠或意外降级
    admins = _split_config_ids(config.get("telegram_admin_user_ids")) - super_admins

    if user_id in super_admins:
        return "super_admin"
    if user_id in admins:
        return "admin"

    if _roles_configured():
        return ""

    if _legacy_allowed(update):
        return "super_admin"
    return ""


def _describe_user(update):
    user = update.effective_user if update else None
    _record_user_if_present(user)
    user_id = str(user.id) if user else ""
    user_name = user.username if user else ""
    full_name = user.full_name if user else ""
    return user_id, user_name, full_name


async def _deny(update, text):
    if update.callback_query:
        await update.callback_query.answer(text, show_alert=True)
    elif update.message:
        await update.message.reply_text(text)


async def check_user_permission(update):
    if not get_user_role(update):
        user_id, user_name, full_name = _describe_user(update)
        logger.warning(f"未授权用户拦截: 用户ID={user_id}，用户名={user_name}，全名={full_name}")
        await _deny(update, "⛔ 未授权的用户：您无权使用此机器人")
        return False

    return True


async def check_super_admin_permission(update):
    role = get_user_role(update)
    if role == "super_admin":
        return True

    user_id, user_name, full_name = _describe_user(update)
    logger.warning(f"越权操作拦截: 用户ID={user_id} (当前身份={role or '未授权'}) 尝试执行超级管理员操作")
    await _deny(update, "⛔ 权限不足：此操作需要【超级管理员】权限，已阻止越权执行")
    return False
