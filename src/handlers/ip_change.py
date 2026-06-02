from telegram import Update
from telegram.ext import ContextTypes

from handlers.user_check import check_user_permission
from services.ip_change_service import perform_ip_change, persist_result_for_notification
from utils.logger import logger
from utils.state import mark_notification_sent, mark_sending_notify


async def change_ip_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_permission(update):
        return

    user_id = update.effective_user.id
    user_name = update.effective_user.username
    full_name = update.effective_user.full_name
    logger.info(f"收到 change 命令，用户ID: {user_id}，用户名: {user_name}，全名: {full_name}")

    await update.message.reply_text(
        "已收到换IP请求，马上开始执行。\n"
        "网络可能会短暂中断；如果结果当时发不出去，机器人恢复后会自动补发。"
    )

    result = await perform_ip_change(trigger="manual")
    text = await persist_result_for_notification(result, chat_id=str(update.effective_chat.id))

    try:
        mark_sending_notify(True)
        await update.message.reply_text(text)
        mark_notification_sent()
    except Exception as e:
        logger.warning(f"发送换IP结果失败，将等待恢复后补发: {e}")
        mark_sending_notify(False)
