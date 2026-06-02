from config import config
from utils.logger import logger


def _split_config_ids(value):
    return {x.strip() for x in str(value or "").split(",") if x.strip()}


async def check_user_permission(update):
    user = update.effective_user
    chat = update.effective_chat
    user_id = str(user.id) if user else ""
    chat_id = str(chat.id) if chat else ""
    user_name = user.username if user else ""
    full_name = user.full_name if user else ""

    configured_users = _split_config_ids(config.get("telegram_allowed_user_ids"))
    configured_chats = _split_config_ids(config.get("telegram_chat_id"))

    if configured_users:
        allowed = user_id in configured_users
    else:
        allowed = user_id in configured_chats or chat_id in configured_chats

    if not allowed:
        logger.warning(f"未授权的用户尝试访问，用户ID: {user_id}，用户名: {user_name}，全名: {full_name}")
        if update.callback_query:
            await update.callback_query.answer("未授权的用户", show_alert=True)
        elif update.message:
            await update.message.reply_text("未授权的用户")
        return False

    return True
