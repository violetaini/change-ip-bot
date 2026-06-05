#!/usr/bin/env python3
import asyncio
import os
import shutil
import socket
import subprocess
import tempfile
import re
from datetime import time as datetime_time
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import config
from handlers.ip_change import change_ip_handler
from handlers.ip_check import check_ip_status
from handlers.ip_quality import (
    crop_report_area,
    extract_svg_url,
    ip_quality_handler,
    render_svg_url_to_png,
    run_quality_command,
)
from handlers.ping import ping_handler
from handlers.speedtest import speedtest_callback, speedtest_handler
from handlers.stream_check import stream_check_handler
from handlers.user_check import check_super_admin_permission, check_user_permission
from services.ip_change_service import perform_ip_change, persist_result_for_notification
from services.dns_update_service import (
    SUPPORTED_DNS_PROVIDERS,
    get_dns_provider_name,
    get_dns_record_name,
    is_dns_update_enabled,
)
from utils.logger import logger
from utils.state import get_pending_notification, mark_notification_sent, mark_sending_notify
from utils.network import get_current_ip
from utils.redact import redact_text


AUTO_CHANGE_JOB_NAME = "auto_change_ip_job"
BEIJING_TZ = ZoneInfo("Asia/Shanghai")

BOT_COMMANDS = [
    BotCommand("start", "显示帮助和可用命令"),
    BotCommand("check", "检查当前IP状态"),
    BotCommand("change", "更换IP并同步华为云DNS"),
    BotCommand("auto_start", "启用自动换IP"),
    BotCommand("auto_stop", "关闭自动换IP"),
    BotCommand("auto_status", "查看自动换IP状态"),
    BotCommand("set_auto_time", "设置自动换IP时间"),
    BotCommand("manage_users", "管理管理员用户"),
    BotCommand("logs", "查看最近运行日志"),
    BotCommand("health", "检查机器人运行状态"),
    BotCommand("dns_status", "查看DNS更新配置"),
    BotCommand("set_dns_provider", "设置DNS服务商"),
    BotCommand("set_dns_record", "设置DNS解析记录"),
    BotCommand("dns_update_on", "启用DNS更新"),
    BotCommand("dns_update_off", "关闭DNS更新"),
    BotCommand("quality", "检测IP质量并发送JPG报告"),
    BotCommand("stream", "检测流媒体解锁并发送简报"),
    BotCommand("ping", "测试网络延迟"),
    BotCommand("speedtest", "测试网络速度"),
]


def persist_config_value(key: str, value) -> None:
    config_path = config.get("_loaded_from")
    if not config_path:
        raise RuntimeError("无法确定当前配置文件路径")

    path = Path(str(config_path))
    lines = path.read_text(encoding="utf-8").splitlines()
    if isinstance(value, bool):
        rendered = "true" if value else "false"
    else:
        rendered = str(value)

    prefix = f"{key}:"
    new_line = f"{key}: {rendered}"
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(prefix) and not stripped.startswith("#"):
            lines[idx] = new_line
            break
    else:
        lines.append(new_line)

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_auto_change_time() -> datetime_time:
    raw_time = str(config.get("auto_change_time", "04:00")).strip()
    try:
        hour_text, minute_text = raw_time.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
        return datetime_time(hour=hour, minute=minute, tzinfo=BEIJING_TZ)
    except ValueError as exc:
        raise ValueError(f"auto_change_time 配置无效，应使用 HH:MM 格式，当前值: {raw_time}") from exc


def resolve_ipv4_records(hostname: str) -> list[str]:
    clean_name = str(hostname or "").strip().rstrip(".")
    if not clean_name:
        return []
    records = socket.getaddrinfo(clean_name, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
    return sorted({item[4][0] for item in records if item and item[4]})


def get_log_path() -> Path:
    return Path(__file__).resolve().parents[1] / "logs" / "bot.log"


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
            "/auto_start - 启用自动换IP\n"
            "/auto_stop - 关闭自动换IP\n"
            "/auto_status - 查看自动换IP状态\n"
            "/set_auto_time HH:MM - 设置自动换IP时间\n"
            "/manage_users - 管理管理员用户（超级管理员）\n"
            "/logs - 查看最近运行日志\n"
            "/health - 检查机器人运行状态\n"
            "/dns_status - 查看DNS更新配置（超级管理员）\n"
            "/set_dns_provider PROVIDER - 设置DNS服务商（超级管理员）\n"
            "/set_dns_record ZONE RECORD [TYPE] [TTL] - 设置DNS解析记录（超级管理员）\n"
            "/dns_update_on - 启用DNS更新（超级管理员）\n"
            "/dns_update_off - 关闭DNS更新（超级管理员）\n"
            "/quality - 检测IP质量并发送JPG报告\n"
            "/stream - 检测流媒体解锁并发送简报\n"
            "/ping - 测试网络延迟\n"
            "/speedtest - 测试网络速度"
        )

    async def auto_change_job(self, context: ContextTypes.DEFAULT_TYPE):
        logger.info("开始执行自动换IP任务")
        retry_count = int(config.get("auto_change_retry_count", 5))
        retry_delay = int(config.get("auto_change_retry_delay_seconds", 60))
        max_attempts = max(1, retry_count + 1)
        result = None

        for attempt in range(1, max_attempts + 1):
            result = await perform_ip_change(trigger="auto")
            if result.success:
                if attempt > 1:
                    result.message = f"{result.message}；自动重试第 {attempt - 1} 次后成功"
                break

            logger.warning(
                f"自动换IP第 {attempt}/{max_attempts} 次失败: "
                f"status={result.status}, message={result.message}"
            )
            if attempt < max_attempts:
                await asyncio.sleep(max(1, retry_delay))

        if result and not result.success and max_attempts > 1:
            result.message = f"{result.message}；已尝试 {max_attempts} 次，仍未成功"

        if not config.get("auto_change_notify", True):
            return

        chat_ids = [x.strip() for x in str(config["telegram_chat_id"]).split(",") if x.strip()]
        if not chat_ids:
            return

        await self.send_change_result_notifications(context, result, chat_ids)

        if result and result.success:
            await self.send_dns_verify_report(context, chat_ids, result.new_ip)
            await self.send_auto_quality_report(context, chat_ids)

    async def send_change_result_notifications(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        result,
        chat_ids: list[str],
    ):
        if not result or not chat_ids:
            return

        text = await persist_result_for_notification(result, chat_id=chat_ids[0])
        failed_chat_ids = []

        for chat_id in chat_ids:
            try:
                await context.bot.send_message(chat_id=chat_id, text=text)
                logger.info(f"已发送自动换IP结果通知: chat_id={chat_id}")
            except Exception as e:
                failed_chat_ids.append(chat_id)
                logger.warning(f"发送自动换IP结果通知失败，将等待恢复后补发: chat_id={chat_id}, error={e}")

        if failed_chat_ids:
            await persist_result_for_notification(result, chat_id=failed_chat_ids[0])
        else:
            mark_notification_sent()

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

    async def send_dns_verify_report(self, context: ContextTypes.DEFAULT_TYPE, chat_ids: list[str], target_ip: str):
        if not config.get("dns_verify_enabled", True):
            return
        if not is_dns_update_enabled():
            return

        record_name = get_dns_record_name().strip().rstrip(".")
        if not record_name or not target_ip:
            return

        delay = int(config.get("dns_verify_delay_seconds", 60))
        retry_count = int(config.get("dns_verify_retry_count", 10))

        for attempt in range(1, retry_count + 1):
            await asyncio.sleep(max(1, delay))
            try:
                records = await asyncio.to_thread(resolve_ipv4_records, record_name)
                if target_ip in records:
                    text = (
                        "DNS解析已生效\n"
                        f"域名: {record_name}\n"
                        f"目标IP: {target_ip}\n"
                        f"当前解析: {', '.join(records)}\n"
                        f"检查次数: {attempt}/{retry_count}"
                    )
                    for chat_id in chat_ids:
                        await context.bot.send_message(chat_id=chat_id, text=text)
                    return

                logger.info(
                    f"DNS解析暂未生效: {record_name}, target={target_ip}, "
                    f"records={records}, attempt={attempt}/{retry_count}"
                )
            except Exception as e:
                records = []
                logger.warning(f"DNS解析检查失败: {e}")

        text = (
            "DNS解析暂未确认生效\n"
            f"域名: {record_name}\n"
            f"目标IP: {target_ip}\n"
            f"最后解析: {', '.join(records) if records else '未获取到A记录'}\n"
            f"已检查: {retry_count} 次"
        )
        for chat_id in chat_ids:
            await context.bot.send_message(chat_id=chat_id, text=text)

    async def send_auto_quality_report(self, context: ContextTypes.DEFAULT_TYPE, chat_ids: list[str]):
        if not config.get("auto_change_quality_report", True):
            return
        if not config.get("ip_quality_enabled", True):
            return

        tmp_dir = None
        try:
            quality_cmd = str(config.get("ip_quality_cmd") or "").strip()
            return_code, output = await asyncio.to_thread(run_quality_command, quality_cmd)
            logger.info(f"自动IP质量检测命令返回码: {return_code}")

            svg_url = extract_svg_url(output)
            if not svg_url:
                text = (
                    "自动IP质量检测完成，但没有识别到SVG链接。\n"
                    f"命令返回码: {return_code}\n\n"
                    f"最近输出:\n{redact_text((output or '无输出')[-1500:])}"
                )
                for chat_id in chat_ids:
                    await context.bot.send_message(chat_id=chat_id, text=text)
                return

            tmp_dir = tempfile.mkdtemp(prefix="auto_ip_quality_")
            png_path = str(Path(tmp_dir) / "ip_quality_report.png")
            jpg_path = str(Path(tmp_dir) / "ip_quality_report.jpg")
            await asyncio.to_thread(render_svg_url_to_png, svg_url, png_path)
            await asyncio.to_thread(crop_report_area, png_path, jpg_path)

            for chat_id in chat_ids:
                with open(jpg_path, "rb") as f:
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=f,
                        caption="自动换IP后的IP质量检测报告",
                    )
        except Exception as e:
            logger.exception(f"自动IP质量检测失败: {e}")
            for chat_id in chat_ids:
                await context.bot.send_message(chat_id=chat_id, text=f"自动IP质量检测失败：{redact_text(str(e))}")
        finally:
            if tmp_dir and os.path.isdir(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)

    async def post_init(self, application: Application):
        try:
            await application.bot.set_my_commands(BOT_COMMANDS)
            logger.info("已注册 Telegram 机器人命令菜单")
        except Exception as e:
            logger.warning(f"注册 Telegram 命令菜单失败: {e}")

    def get_auto_change_jobs(self):
        if not self.app or not self.app.job_queue:
            return []
        return self.app.job_queue.get_jobs_by_name(AUTO_CHANGE_JOB_NAME)

    def schedule_auto_change_job(self) -> bool:
        if not self.app or not self.app.job_queue:
            logger.warning("JobQueue 不可用，请确认安装了 python-telegram-bot[job-queue]")
            return False

        try:
            run_time = parse_auto_change_time()
        except ValueError as e:
            logger.warning(str(e))
            return False

        if self.get_auto_change_jobs():
            logger.info("自动换IP任务已存在，跳过重复注册")
            return True

        self.app.job_queue.run_daily(
            self.auto_change_job,
            time=run_time,
            name=AUTO_CHANGE_JOB_NAME,
        )
        logger.info(f"自动换IP任务已注册，每天北京时间 {run_time.strftime('%H:%M')} 执行一次")
        return True

    def cancel_auto_change_jobs(self) -> int:
        jobs = self.get_auto_change_jobs()
        for job in jobs:
            job.schedule_removal()
        if jobs:
            logger.info(f"已移除 {len(jobs)} 个自动换IP任务")
        return len(jobs)

    async def auto_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_super_admin_permission(update):
            return

        try:
            config["auto_change_enabled"] = True
            persist_config_value("auto_change_enabled", True)
            scheduled = self.schedule_auto_change_job()
        except Exception as e:
            logger.exception(f"启用自动换IP失败: {e}")
            await update.message.reply_text(f"启用自动换IP失败：{redact_text(str(e))}")
            return

        if scheduled:
            run_time = parse_auto_change_time()
            await update.message.reply_text(f"已启用自动换IP，每天北京时间 {run_time.strftime('%H:%M')} 执行一次。")
        else:
            await update.message.reply_text("已写入启用配置，但当前 JobQueue 不可用，未注册定时任务。")

    async def auto_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_super_admin_permission(update):
            return

        try:
            config["auto_change_enabled"] = False
            persist_config_value("auto_change_enabled", False)
            removed = self.cancel_auto_change_jobs()
        except Exception as e:
            logger.exception(f"关闭自动换IP失败: {e}")
            await update.message.reply_text(f"关闭自动换IP失败：{redact_text(str(e))}")
            return

        await update.message.reply_text(f"已关闭自动换IP，已移除 {removed} 个运行中的定时任务。")

    async def auto_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_user_permission(update):
            return

        enabled = bool(config.get("auto_change_enabled"))
        jobs = self.get_auto_change_jobs()
        auto_time = str(config.get("auto_change_time", "04:00")).strip()
        retry_count = int(config.get("auto_change_retry_count", 5))
        retry_delay = int(config.get("auto_change_retry_delay_seconds", 60))
        await update.message.reply_text(
            "自动换IP状态\n"
            f"配置状态: {'已启用' if enabled else '已关闭'}\n"
            f"定时任务: {'运行中' if jobs else '未注册'}\n"
            f"执行时间: 每天北京时间 {auto_time}\n"
            f"失败重试: 最多 {retry_count} 次，间隔 {retry_delay} 秒"
        )

    async def set_auto_time(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_super_admin_permission(update):
            return

        if not context.args:
            await update.message.reply_text("用法: /set_auto_time HH:MM，例如 /set_auto_time 04:00")
            return

        new_time = context.args[0].strip()
        old_time = config.get("auto_change_time", "04:00")
        try:
            config["auto_change_time"] = new_time
            parse_auto_change_time()
            persist_config_value("auto_change_time", f'"{new_time}"')

            if config.get("auto_change_enabled"):
                self.cancel_auto_change_jobs()
                scheduled = self.schedule_auto_change_job()
            else:
                scheduled = False
        except Exception as e:
            config["auto_change_time"] = old_time
            logger.exception(f"设置自动换IP时间失败: {e}")
            await update.message.reply_text(f"设置自动换IP时间失败：{redact_text(str(e))}")
            return

        if scheduled:
            await update.message.reply_text(f"已设置自动换IP时间为每天北京时间 {new_time}，定时任务已重新注册。")
        else:
            await update.message.reply_text(f"已设置自动换IP时间为每天北京时间 {new_time}。当前自动换IP未启用。")

    async def add_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_super_admin_permission(update):
            return

        if not context.args:
            await update.message.reply_text("用法: /add_admin USER_ID，例如 /add_admin 123456789")
            return

        new_admin_id = context.args[0].strip()
        if not re.fullmatch(r"\d{5,20}", new_admin_id):
            await update.message.reply_text("管理员 USER_ID 格式无效，应为 5-20 位数字。")
            return

        super_admin_ids = [
            x.strip() for x in str(config.get("telegram_super_admin_user_ids", "")).split(",") if x.strip()
        ]
        admin_ids = [x.strip() for x in str(config.get("telegram_admin_user_ids", "")).split(",") if x.strip()]

        if new_admin_id in super_admin_ids:
            await update.message.reply_text("该用户已经是超级管理员。")
            return
        if new_admin_id in admin_ids:
            await update.message.reply_text("该用户已经是普通管理员。")
            return

        admin_ids.append(new_admin_id)
        rendered_admins = ",".join(admin_ids)
        try:
            config["telegram_admin_user_ids"] = rendered_admins
            persist_config_value("telegram_admin_user_ids", rendered_admins)
        except Exception as e:
            logger.exception(f"添加普通管理员失败: {e}")
            await update.message.reply_text(f"添加普通管理员失败：{redact_text(str(e))}")
            return

        await update.message.reply_text(f"已添加普通管理员: {new_admin_id}")

    def user_management_text(self) -> str:
        super_admin_ids = [
            x.strip() for x in str(config.get("telegram_super_admin_user_ids", "")).split(",") if x.strip()
        ]
        admin_ids = [x.strip() for x in str(config.get("telegram_admin_user_ids", "")).split(",") if x.strip()]
        super_text = "\n".join(f"- {user_id}" for user_id in super_admin_ids) or "- 未配置"
        admin_text = "\n".join(f"- {user_id}" for user_id in admin_ids) or "- 暂无"
        return (
            "用户管理\n"
            "超级管理员:\n"
            f"{super_text}\n\n"
            "普通管理员:\n"
            f"{admin_text}"
        )

    def user_management_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("添加普通管理员", callback_data="manage_users:add"),
                InlineKeyboardButton("删除普通管理员", callback_data="manage_users:delete_menu"),
            ],
            [InlineKeyboardButton("刷新列表", callback_data="manage_users:menu")],
        ])

    def delete_admin_keyboard(self) -> InlineKeyboardMarkup:
        admin_ids = [x.strip() for x in str(config.get("telegram_admin_user_ids", "")).split(",") if x.strip()]
        rows = [
            [InlineKeyboardButton(f"删除 {admin_id}", callback_data=f"manage_users:delete:{admin_id}")]
            for admin_id in admin_ids
        ]
        rows.append([InlineKeyboardButton("返回", callback_data="manage_users:menu")])
        return InlineKeyboardMarkup(rows)

    async def manage_users(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_super_admin_permission(update):
            return

        context.user_data.pop("awaiting_add_admin", None)
        await update.message.reply_text(
            self.user_management_text(),
            reply_markup=self.user_management_keyboard(),
        )

    async def manage_users_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_super_admin_permission(update):
            return

        query = update.callback_query
        await query.answer()
        data = query.data or ""

        if data == "manage_users:menu":
            context.user_data.pop("awaiting_add_admin", None)
            await query.edit_message_text(
                self.user_management_text(),
                reply_markup=self.user_management_keyboard(),
            )
            return

        if data == "manage_users:add":
            context.user_data["awaiting_add_admin"] = True
            await query.edit_message_text(
                "请发送要添加的 Telegram USER_ID。\n"
                "只接受 5-20 位数字。\n\n"
                "发送 /manage_users 可取消并返回用户管理。",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("返回", callback_data="manage_users:menu")]
                ]),
            )
            return

        if data == "manage_users:delete_menu":
            admin_ids = [x.strip() for x in str(config.get("telegram_admin_user_ids", "")).split(",") if x.strip()]
            if not admin_ids:
                await query.edit_message_text(
                    "当前没有普通管理员。",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("返回", callback_data="manage_users:menu")]
                    ]),
                )
                return

            await query.edit_message_text(
                "请选择要删除的普通管理员：",
                reply_markup=self.delete_admin_keyboard(),
            )
            return

        if data.startswith("manage_users:delete:"):
            admin_id = data.split(":", 2)[2].strip()
            admin_ids = [x.strip() for x in str(config.get("telegram_admin_user_ids", "")).split(",") if x.strip()]
            if admin_id in admin_ids:
                admin_ids = [x for x in admin_ids if x != admin_id]
                rendered_admins = ",".join(admin_ids)
                try:
                    config["telegram_admin_user_ids"] = rendered_admins
                    persist_config_value("telegram_admin_user_ids", rendered_admins)
                except Exception as e:
                    logger.exception(f"删除普通管理员失败: {e}")
                    await query.edit_message_text(f"删除普通管理员失败：{redact_text(str(e))}")
                    return

            await query.edit_message_text(
                f"已删除普通管理员: {admin_id}\n\n{self.user_management_text()}",
                reply_markup=self.user_management_keyboard(),
            )
            return

    async def remove_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_super_admin_permission(update):
            return

        admin_ids = [x.strip() for x in str(config.get("telegram_admin_user_ids", "")).split(",") if x.strip()]
        if not admin_ids:
            await update.message.reply_text("当前没有普通管理员。")
            return

        keyboard = [
            [InlineKeyboardButton(f"删除 {admin_id}", callback_data=f"remove_admin:{admin_id}")]
            for admin_id in admin_ids
        ]
        await update.message.reply_text(
            "请选择要删除的普通管理员：",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def remove_admin_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_super_admin_permission(update):
            return

        query = update.callback_query
        await query.answer()

        admin_id = (query.data or "").split(":", 1)[1].strip()
        admin_ids = [x.strip() for x in str(config.get("telegram_admin_user_ids", "")).split(",") if x.strip()]
        if admin_id not in admin_ids:
            await query.edit_message_text(f"该用户已经不是普通管理员: {admin_id}")
            return

        admin_ids = [x for x in admin_ids if x != admin_id]
        rendered_admins = ",".join(admin_ids)
        try:
            config["telegram_admin_user_ids"] = rendered_admins
            persist_config_value("telegram_admin_user_ids", rendered_admins)
        except Exception as e:
            logger.exception(f"删除普通管理员失败: {e}")
            await query.edit_message_text(f"删除普通管理员失败：{redact_text(str(e))}")
            return

        await query.edit_message_text(f"已删除普通管理员: {admin_id}")

    async def manage_users_add_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.user_data.get("awaiting_add_admin"):
            return
        if not await check_super_admin_permission(update):
            context.user_data.pop("awaiting_add_admin", None)
            return

        new_admin_id = (update.message.text or "").strip()
        if not re.fullmatch(r"\d{5,20}", new_admin_id):
            await update.message.reply_text("USER_ID 格式无效，请发送 5-20 位数字，或发送 /manage_users 返回。")
            return

        super_admin_ids = [
            x.strip() for x in str(config.get("telegram_super_admin_user_ids", "")).split(",") if x.strip()
        ]
        admin_ids = [x.strip() for x in str(config.get("telegram_admin_user_ids", "")).split(",") if x.strip()]

        context.user_data.pop("awaiting_add_admin", None)
        if new_admin_id in super_admin_ids:
            await update.message.reply_text(
                "该用户已经是超级管理员。\n\n" + self.user_management_text(),
                reply_markup=self.user_management_keyboard(),
            )
            return
        if new_admin_id not in admin_ids:
            admin_ids.append(new_admin_id)
            rendered_admins = ",".join(admin_ids)
            try:
                config["telegram_admin_user_ids"] = rendered_admins
                persist_config_value("telegram_admin_user_ids", rendered_admins)
            except Exception as e:
                logger.exception(f"添加普通管理员失败: {e}")
                await update.message.reply_text(f"添加普通管理员失败：{redact_text(str(e))}")
                return

        await update.message.reply_text(
            f"已添加普通管理员: {new_admin_id}\n\n{self.user_management_text()}",
            reply_markup=self.user_management_keyboard(),
        )

    async def logs(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_super_admin_permission(update):
            return

        try:
            limit = int(context.args[0]) if context.args else 50
        except ValueError:
            limit = 50
        limit = max(1, min(limit, 100))

        log_path = get_log_path()
        if not log_path.exists():
            await update.message.reply_text(f"日志文件不存在: {log_path}")
            return

        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
            text = redact_text("\n".join(lines) or "日志为空")
            if len(text) > 3500:
                text = text[-3500:]
            await update.message.reply_text(f"最近 {limit} 行日志:\n{text}")
        except Exception as e:
            logger.exception(f"读取日志失败: {e}")
            await update.message.reply_text(f"读取日志失败：{redact_text(str(e))}")

    async def dns_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_super_admin_permission(update):
            return

        provider = get_dns_provider_name() or "未配置"
        zone_name = str(config.get("dns_zone_name") or config.get("huawei_dns_zone_name", "")).strip() or "未配置"
        record_name = get_dns_record_name().strip() or "未配置"
        record_type = str(config.get("dns_record_type") or config.get("huawei_dns_record_type", "A")).strip().upper()
        ttl = int(config.get("dns_ttl") or config.get("huawei_dns_ttl", 60))

        await update.message.reply_text(
            "DNS更新配置\n"
            f"状态: {'已启用' if is_dns_update_enabled() else '未启用'}\n"
            f"服务商: {provider}\n"
            f"Zone: {zone_name}\n"
            f"记录: {record_name}\n"
            f"类型: {record_type}\n"
            f"TTL: {ttl}\n"
            f"支持: {', '.join(SUPPORTED_DNS_PROVIDERS)}"
        )

    async def set_dns_provider(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_super_admin_permission(update):
            return

        if not context.args:
            await update.message.reply_text(
                "用法: /set_dns_provider PROVIDER\n"
                f"支持: {', '.join(SUPPORTED_DNS_PROVIDERS)}"
            )
            return

        provider = context.args[0].strip().lower()
        if provider not in SUPPORTED_DNS_PROVIDERS:
            await update.message.reply_text(
                f"不支持的DNS服务商: {provider}\n"
                f"支持: {', '.join(SUPPORTED_DNS_PROVIDERS)}"
            )
            return

        try:
            config["dns_provider"] = provider
            config["dns_update_enabled"] = True
            persist_config_value("dns_provider", provider)
            persist_config_value("dns_update_enabled", True)
        except Exception as e:
            logger.exception(f"设置DNS服务商失败: {e}")
            await update.message.reply_text(f"设置DNS服务商失败：{redact_text(str(e))}")
            return

        await update.message.reply_text(f"已设置DNS服务商为 {provider}，并启用DNS更新。")

    async def set_dns_record(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_super_admin_permission(update):
            return

        if len(context.args) < 2:
            await update.message.reply_text(
                "用法: /set_dns_record ZONE RECORD [TYPE] [TTL]\n"
                "例如: /set_dns_record ascf.eu.org seed.ascf.eu.org A 60"
            )
            return

        zone_name = context.args[0].strip().rstrip(".")
        record_name = context.args[1].strip().rstrip(".")
        record_type = (context.args[2].strip().upper() if len(context.args) >= 3 else str(
            config.get("dns_record_type") or config.get("huawei_dns_record_type", "A")
        ).strip().upper())
        ttl = int(config.get("dns_ttl") or config.get("huawei_dns_ttl", 60))

        if len(context.args) >= 4:
            try:
                ttl = int(context.args[3])
            except ValueError:
                await update.message.reply_text("TTL 必须是数字。")
                return

        if not re.fullmatch(r"[A-Za-z0-9_.-]+", zone_name) or "." not in zone_name:
            await update.message.reply_text("ZONE 格式无效，例如 ascf.eu.org")
            return
        if not re.fullmatch(r"[A-Za-z0-9_.*-]+(?:\.[A-Za-z0-9_.*-]+)+", record_name):
            await update.message.reply_text("RECORD 格式无效，例如 seed.ascf.eu.org")
            return
        if record_type not in {"A", "AAAA"}:
            await update.message.reply_text("当前只支持 A 或 AAAA 记录。")
            return
        if not (1 <= ttl <= 86400):
            await update.message.reply_text("TTL 应在 1 到 86400 秒之间。")
            return

        try:
            updates = {
                "dns_zone_name": zone_name,
                "dns_record_name": record_name,
                "dns_record_type": record_type,
                "dns_ttl": ttl,
                "huawei_dns_zone_name": zone_name,
                "huawei_dns_record_name": record_name,
                "huawei_dns_record_type": record_type,
                "huawei_dns_ttl": ttl,
            }
            for key, value in updates.items():
                config[key] = value
                persist_config_value(key, value)
        except Exception as e:
            logger.exception(f"设置DNS解析记录失败: {e}")
            await update.message.reply_text(f"设置DNS解析记录失败：{redact_text(str(e))}")
            return

        await update.message.reply_text(
            "已设置DNS解析记录\n"
            f"Zone: {zone_name}\n"
            f"记录: {record_name}\n"
            f"类型: {record_type}\n"
            f"TTL: {ttl}"
        )

    async def dns_update_on(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_super_admin_permission(update):
            return

        provider = get_dns_provider_name()
        if not provider:
            await update.message.reply_text(
                "尚未配置DNS服务商。请先使用 /set_dns_provider PROVIDER。"
            )
            return

        try:
            config["dns_update_enabled"] = True
            persist_config_value("dns_update_enabled", True)
        except Exception as e:
            logger.exception(f"启用DNS更新失败: {e}")
            await update.message.reply_text(f"启用DNS更新失败：{redact_text(str(e))}")
            return

        await update.message.reply_text(f"已启用DNS更新，当前服务商: {provider}")

    async def dns_update_off(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_super_admin_permission(update):
            return

        try:
            config["dns_update_enabled"] = False
            config["huawei_dns_enabled"] = False
            persist_config_value("dns_update_enabled", False)
            persist_config_value("huawei_dns_enabled", False)
        except Exception as e:
            logger.exception(f"关闭DNS更新失败: {e}")
            await update.message.reply_text(f"关闭DNS更新失败：{redact_text(str(e))}")
            return

        await update.message.reply_text("已关闭DNS更新。")

    async def health(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_user_permission(update):
            return

        checks = []

        try:
            current_ip = await asyncio.to_thread(get_current_ip)
            checks.append(f"公网IP: {current_ip}")
        except Exception as e:
            checks.append(f"公网IP: 获取失败 ({e})")

        checks.append(f"换IP API: {'已配置' if str(config.get('ip_change_api', '')).strip() else '未配置'}")
        checks.append(f"自动换IP: {'已启用' if config.get('auto_change_enabled') else '已关闭'}")
        checks.append(f"自动时间: 每天北京时间 {config.get('auto_change_time', '04:00')}")
        checks.append(
            "自动重试: "
            f"最多 {int(config.get('auto_change_retry_count', 5))} 次，"
            f"间隔 {int(config.get('auto_change_retry_delay_seconds', 60))} 秒"
        )
        dns_provider = get_dns_provider_name() or "未配置"
        checks.append(f"DNS更新: {'已启用' if is_dns_update_enabled() else '未启用'} ({dns_provider})")

        record_name = get_dns_record_name().strip()
        if is_dns_update_enabled() and record_name:
            try:
                records = await asyncio.to_thread(resolve_ipv4_records, record_name)
                checks.append(f"DNS解析: {record_name} -> {', '.join(records) if records else '无A记录'}")
            except Exception as e:
                checks.append(f"DNS解析: 检查失败 ({e})")

        state_file = Path(str(config.get("state_file", "/var/lib/vps-ip-bot/state.json"))).expanduser()
        state_parent = state_file.parent
        checks.append(f"状态文件目录: {'可写' if os.access(state_parent, os.W_OK) else '不可写'} ({state_parent})")

        quality_tool = "Chromium" if any(shutil.which(name) for name in (
            "chromium",
            "chromium-browser",
            "google-chrome",
            "google-chrome-stable",
        )) else "CairoSVG"
        checks.append(f"IP质量图片渲染: {quality_tool}")

        stream_tools = []
        for name in ("bash", "curl"):
            stream_tools.append(f"{name}:{'可用' if shutil.which(name) else '不可用'}")
        checks.append(
            "流媒体检测: "
            f"{'已启用' if config.get('stream_check_enabled') else '已关闭'} "
            f"({', '.join(stream_tools)})"
        )

        speedtest_cli = "可用" if shutil.which("speedtest") else "不可用"
        checks.append(f"speedtest CLI: {speedtest_cli}")

        await update.message.reply_text("健康检查\n" + "\n".join(f"- {item}" for item in checks))

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

        self.schedule_auto_change_job()

    def run(self):
        logger.info("机器人初始化中...")
        self.app = ApplicationBuilder().token(config["telegram_bot_token"]).post_init(self.post_init).build()

        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(CommandHandler("check", check_ip_status))
        self.app.add_handler(CommandHandler("change", change_ip_handler))
        self.app.add_handler(CommandHandler("auto_start", self.auto_start))
        self.app.add_handler(CommandHandler("auto_stop", self.auto_stop))
        self.app.add_handler(CommandHandler("auto_status", self.auto_status))
        self.app.add_handler(CommandHandler("set_auto_time", self.set_auto_time))
        self.app.add_handler(CommandHandler("manage_users", self.manage_users))
        self.app.add_handler(CommandHandler("add_admin", self.add_admin))
        self.app.add_handler(CommandHandler("remove_admin", self.remove_admin))
        self.app.add_handler(CommandHandler("logs", self.logs))
        self.app.add_handler(CommandHandler("health", self.health))
        self.app.add_handler(CommandHandler("dns_status", self.dns_status))
        self.app.add_handler(CommandHandler("set_dns_provider", self.set_dns_provider))
        self.app.add_handler(CommandHandler("set_dns_record", self.set_dns_record))
        self.app.add_handler(CommandHandler("dns_update_on", self.dns_update_on))
        self.app.add_handler(CommandHandler("dns_update_off", self.dns_update_off))
        self.app.add_handler(CommandHandler("quality", ip_quality_handler))
        self.app.add_handler(CommandHandler("stream", stream_check_handler))
        self.app.add_handler(CommandHandler("ping", ping_handler))
        self.app.add_handler(CommandHandler("speedtest", speedtest_handler))
        self.app.add_handler(CallbackQueryHandler(self.manage_users_callback, pattern="^manage_users:"))
        self.app.add_handler(CallbackQueryHandler(self.remove_admin_callback, pattern="^remove_admin:"))
        self.app.add_handler(CallbackQueryHandler(speedtest_callback, pattern="^speedtest_"))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.manage_users_add_text))

        self.setup_jobs()

        logger.info("机器人开始运行")
        self.app.run_polling(drop_pending_updates=True)


def main():
    bot = VPSChangeIPBot()
    bot.run()


if __name__ == "__main__":
    main()
