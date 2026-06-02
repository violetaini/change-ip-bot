#!/usr/bin/env python3
from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from config import config
from handlers.ip_change import change_ip_handler
from handlers.ip_check import check_ip_status
from handlers.ip_quality import ip_quality_handler
from handlers.ping import ping_handler
from handlers.speedtest import speedtest_callback, speedtest_handler
from handlers.user_check import check_user_permission
from services.ip_change_service import perform_ip_change, persist_result_for_notification
from utils.logger import logger
from utils.state import get_pending_notification, mark_notification_sent, mark_sending_notify


BOT_COMMANDS = [
    BotCommand("start", "显示帮助和可用命令"),
    BotCommand("check", "检查当前IP状态"),
    BotCommand("change", "更换IP并同步华为云DNS"),
    BotCommand("quality", "检测IP质量并发送JPG报告"),
    BotCommand("ping", "测试网络延迟"),
    BotCommand("speedtest", "测试网络速度"),
]


class VPSChangeIPBot:
    def __init__(self):
        self.app = None

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_user_permission(update):
            return

        await update.message.reply_text(
            "欢迎使用 VPS IP 更换工具\n"
            "/start - 显示帮助\n"
            "/check - 检查当前IP状态\n"
            "/change - 更换IP并同步华为云DNS\n"
            "/quality - 检测IP质量并发送JPG报告\n"
            "/ping - 测试网络延迟\n"
            "/speedtest - 测试网络速度"
        )

    async def auto_change_job(self, context: ContextTypes.DEFAULT_TYPE):
        logger.info("开始执行自动换IP任务")
        result = await perform_ip_change(trigger="auto")

        if not config.get("auto_change_notify", True):
            return

        chat_ids = [x.strip() for x in str(config["telegram_chat_id"]).split(",") if x.strip()]
        if not chat_ids:
            return

        for chat_id in chat_ids:
            await persist_result_for_notification(result, chat_id=chat_id)
        await self.try_send_pending_notifications(context)

    async def try_send_pending_notifications(self, context: ContextTypes.DEFAULT_TYPE):
        pending = get_pending_notification()
        if not pending:
            return
        if pending.get("sending_notify"):
            return

        chat_id = pending.get("last_chat_id") or str(config.get("telegram_chat_id", "")).split(",")[0].strip()
        message = pending.get("last_message", "")
        if not chat_id or not message:
            return

        try:
            mark_sending_notify(True)
            await context.bot.send_message(chat_id=chat_id, text=message)
            mark_notification_sent()
            logger.info("已补发上次未送达的换IP结果通知")
        except Exception as e:
            mark_sending_notify(False)
            logger.warning(f"补发换IP结果通知失败，稍后重试: {e}")

    async def post_init(self, application: Application):
        try:
            await application.bot.set_my_commands(BOT_COMMANDS)
            logger.info("已注册 Telegram 机器人命令菜单")
        except Exception as e:
            logger.warning(f"注册 Telegram 命令菜单失败: {e}")

    def setup_jobs(self):
        if self.app.job_queue:
            self.app.job_queue.run_repeating(
                self.try_send_pending_notifications,
                interval=30,
                first=10,
                name="pending_notification_job",
            )

        if not config.get("auto_change_enabled"):
            logger.info("自动换IP未启用")
            return

        minutes = int(config.get("auto_change_interval_minutes", 360))
        if minutes < 1:
            logger.warning("auto_change_interval_minutes 配置无效，跳过注册")
            return

        if not self.app.job_queue:
            logger.warning("JobQueue 不可用，请确认安装了 python-telegram-bot[job-queue]")
            return

        self.app.job_queue.run_repeating(
            self.auto_change_job,
            interval=minutes * 60,
            first=30,
            name="auto_change_ip_job",
        )
        logger.info(f"自动换IP任务已注册，每 {minutes} 分钟执行一次")

    def run(self):
        logger.info("机器人初始化中...")
        self.app = ApplicationBuilder().token(config["telegram_bot_token"]).post_init(self.post_init).build()

        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(CommandHandler("check", check_ip_status))
        self.app.add_handler(CommandHandler("change", change_ip_handler))
        self.app.add_handler(CommandHandler("quality", ip_quality_handler))
        self.app.add_handler(CommandHandler("ping", ping_handler))
        self.app.add_handler(CommandHandler("speedtest", speedtest_handler))
        self.app.add_handler(CallbackQueryHandler(speedtest_callback, pattern="^speedtest_"))

        self.setup_jobs()

        logger.info("机器人开始运行")
        self.app.run_polling(drop_pending_updates=True)


def main():
    bot = VPSChangeIPBot()
    bot.run()


if __name__ == "__main__":
    main()
