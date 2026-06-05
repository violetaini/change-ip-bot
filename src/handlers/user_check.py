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


def get_user_role(update):
    user = update.effective_user
    user_id = str(user.id) if user else ""

    super_admins = _split_config_ids(config.get("telegram_super_admin_user_ids"))
    admins = _split_config_ids(config.get("telegram_admin_user_ids"))

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
    user = update.effective_user
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
        logger.warning(f"未授权的用户尝试访问，用户ID: {user_id}，用户名: {user_name}，全名: {full_name}")
        await _deny(update, "未授权的用户")
        return False

    return True


async def check_super_admin_permission(update):
    if get_user_role(update) == "super_admin":
        return True

    user_id, user_name, full_name = _describe_user(update)
    logger.warning(f"非超级管理员尝试执行敏感操作，用户ID: {user_id}，用户名: {user_name}，全名: {full_name}")
    await _deny(update, "需要超级管理员权限")
    return False
