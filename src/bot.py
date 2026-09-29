#!/usr/bin/env python3
import asyncio
import os
import shutil
import socket
import subprocess
import tempfile
import time
import re
import datetime as dt
from datetime import time as datetime_time
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from telegram import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import config, get_server_config, get_servers, is_multi_server_mode
from handlers.ip_change import change_ip_handler, do_change_single_server
from handlers.ip_check import check_ip_status, do_check_single_server
from handlers.ip_quality import (
    crop_report_area,
    extract_svg_url,
    extract_svg_urls,
    ip_quality_handler,
    render_svg_url_to_png,
    run_quality_command,
)
from handlers.ping import ping_handler
from handlers.server_selection import resolve_target_server
from handlers.speedtest import speedtest_callback, speedtest_handler
from handlers.stream_check import stream_check_handler
from handlers.user_check import check_super_admin_permission, check_user_permission
from services.ip_change_service import (
    build_result_message,
    perform_ip_change,
    persist_result_for_notification,
)
from services.dns_update_service import (
    SUPPORTED_DNS_PROVIDERS,
    get_dns_provider_name,
    get_dns_record_name,
    is_dns_update_enabled,
)
from utils.logger import logger
from utils.system_deps import ensure_system_dependencies, get_system_dependency_status
from utils.state import (
    get_all_pending_notifications,
    get_last_change_time,
    get_pending_notification,
    get_user_selected_server,
    load_server_state,
    load_state,
    mark_notification_sent,
    mark_sending_notify,
    set_user_selected_server,
)
from utils.network import call_boil_get_ip, get_current_ip
from utils.redact import redact_text


AUTO_CHANGE_JOB_NAME = "auto_change_ip_job"
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
INVISIBLE_MESSAGE_TEXT = "\u2063"

BOT_COMMANDS = [
    BotCommand("start", "显示帮助和可用命令"),
    BotCommand("servers", "查看所有服务器状态看板"),
    BotCommand("use", "切换当前默认操作服务器"),
    BotCommand("check", "检查当前IP状态"),
    BotCommand("change", "更换IP并同步DNS"),
    BotCommand("ip_status", "查看换IP配置与冷却状态"),
    BotCommand("set_ip_mode", "切换换IP模式(通用/Fachost/Boil)"),
    BotCommand("set_boil_token", "设置Boil API Token"),
    BotCommand("set_ip_api", "设置换IP接口URL"),
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
    BotCommand("ping", "测试网络延迟(支持IPv4/IPv6)"),
    BotCommand("speedtest", "测试网络速度"),
]


def _get_admin_id_list() -> list[str]:
    val = config.get("telegram_admin_user_ids")
    return [x.strip() for x in str(val or "").split(",") if x.strip()]


def _get_super_admin_id_list() -> list[str]:
    val = config.get("telegram_super_admin_user_ids")
    return [x.strip() for x in str(val or "").split(",") if x.strip()]


def persist_config_value(key: str, value) -> None:
    config_path = config.get("_loaded_from")
    if not config_path:
        raise RuntimeError("无法确定当前配置文件路径")

    path = Path(str(config_path))
    lines = path.read_text(encoding="utf-8").splitlines()
    if isinstance(value, bool):
        rendered = "true" if value else "false"
    elif isinstance(value, (int, float)):
        rendered = str(value)
    elif value is None:
        rendered = '""'
    else:
        val_str = str(value).strip()
        if (val_str.startswith('"') and val_str.endswith('"')) or (val_str.startswith("'") and val_str.endswith("'")):
            rendered = val_str
        else:
            rendered = f'"{val_str}"'

    prefix = f"{key}:"
    found = False
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(prefix) and not stripped.startswith("#"):
            indent = line[: len(line) - len(line.lstrip())]
            # 严格匹配顶级配置项，避免误修改 servers 节点块内部同名子配置
            if not indent:
                lines[idx] = f"{key}: {rendered}"
                found = True
                break
    if not found:
        lines.append(f"{key}: {rendered}")

    temp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}.{time.perf_counter_ns()}")
    try:
        temp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        temp_path.replace(path)
    except Exception:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass
        raise


def parse_auto_change_time(raw_val: Optional[str] = None) -> datetime_time:
    raw_time = str(raw_val if raw_val is not None else config.get("auto_change_time", "04:00")).strip()
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

    # 1. 优先使用 Cloudflare 1.1.1.1 DoH 解析，绕过本地 SmartDNS / AdGuard 缓存
    try:
        from utils.remote_ssh import resolve_via_cloudflare_doh
        doh_ip = resolve_via_cloudflare_doh(clean_name, timeout=5)
        if doh_ip:
            return [doh_ip]
    except Exception:
        pass

    # 2. 本地系统解析兜底
    try:
        records = socket.getaddrinfo(clean_name, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
        return sorted({item[4][0] for item in records if item and item[4]})
    except Exception:
        return []


def get_log_path() -> Path:
    return Path(__file__).resolve().parents[1] / "logs" / "bot.log"


class VPSChangeIPBot:
    def __init__(self):
        self.app = None

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_user_permission(update):
            return

        is_multi = is_multi_server_mode()
        if is_multi:
            ops_section = (
                "【节点管理】\n"
                "/servers - 查看所有服务器状态看板\n"
                "/use [节点] - 切换当前默认操作服务器\n"
                "/check [节点/all] - 检查当前IP与境内连通性\n"
                "/change [节点/all] - 更换IP并同步DNS\n"
                "/ip_status [节点/all] - 查看换IP配置与冷却状态\n\n"
                "【网络诊断】\n"
                "/quality [节点] [-4/-6] - 检测IP质量并生成JPG报告\n"
                "/stream [节点] - 检测流媒体解锁并发送简报\n"
                "/ping [节点] [-4/-6] [目标] [-c 次数] - 测试网络延迟Ping\n"
                "/speedtest [节点] - Ookla网络速度测速\n\n"
            )
        else:
            ops_section = (
                "【常用操作】\n"
                "/check - 检查当前IP与境内连通性\n"
                "/change - 更换IP并同步DNS\n"
                "/ip_status - 查看换IP配置与冷却状态\n\n"
                "【网络诊断】\n"
                "/quality [-4/-6] - 检测IP质量并生成JPG报告\n"
                "/stream - 检测流媒体解锁并发送简报\n"
                "/ping [-4/-6] [目标] [-c 次数] - 测试网络延迟Ping\n"
                "/speedtest - Ookla网络速度测速\n\n"
            )

        await update.message.reply_text(
            "欢迎使用 VPS IP 更换与网络运维工具\n\n"
            f"{ops_section}"
            "【定时与运维】\n"
            "/health - 检查机器人及节点运行状态\n"
            "/auto_status - 查看自动换IP定时状态\n"
            "/auto_start - 启用自动换IP（超级管理员）\n"
            "/auto_stop - 关闭自动换IP（超级管理员）\n"
            "/set_auto_time HH:MM - 设置自动换IP时间（超级管理员）\n\n"
            "【系统与DNS配置（超级管理员）】\n"
            "/dns_status - 查看DNS更新配置\n"
            "/set_dns_provider PROVIDER - 设置DNS服务商\n"
            "/set_dns_record ZONE RECORD [TYPE] [TTL] - 设置DNS解析记录\n"
            "/dns_update_on / /dns_update_off - 启闭DNS更新\n"
            "/set_ip_mode - 切换换IP模式(通用/Fachost/Boil)\n"
            "/set_boil_token TOKEN - 设置Boil API Token\n"
            "/set_ip_api URL - 设置换IP接口URL\n"
            "/manage_users - 管理管理员用户\n"
            "/logs - 查看最近运行日志"
        )

    async def auto_change_job(self, context: ContextTypes.DEFAULT_TYPE):
        job_data = context.job.data if context.job and isinstance(context.job.data, dict) else {}
        server_id = job_data.get("server_id")
        server_cfg = get_server_config(server_id) if server_id else None

        sid = server_cfg.get("id", "default") if server_cfg else "default"
        sname = server_cfg.get("name", sid) if server_cfg else sid
        logger.info(f"开始执行【{sname}】({sid}) 自动换IP任务")

        cfg = server_cfg if server_cfg is not None else config
        retry_count = int(cfg.get("auto_change_retry_count", 5))
        retry_delay = int(cfg.get("auto_change_retry_delay_seconds", 60))
        max_attempts = max(1, retry_count + 1)
        result = None

        for attempt in range(1, max_attempts + 1):
            result = await perform_ip_change(trigger="auto", server_config=server_cfg)
            if result.success:
                if attempt > 1:
                    result.message = f"{result.message}；自动重试第 {attempt - 1} 次后成功"
                break

            logger.warning(
                f"【{sname}】自动换IP第 {attempt}/{max_attempts} 次失败: "
                f"status={result.status}, message={result.message}"
            )
            if attempt < max_attempts:
                await asyncio.sleep(max(1, retry_delay))

        if result and not result.success and max_attempts > 1:
            result.message = f"{result.message}；已尝试 {max_attempts} 次，仍未成功"

        if not cfg.get("auto_change_notify", True):
            return

        chat_ids = [x.strip() for x in str(cfg.get("telegram_chat_id", config.get("telegram_chat_id", ""))).split(",") if x.strip()]
        if not chat_ids:
            return

        await self.send_change_result_notifications(context, result, chat_ids)

        if result and result.success:
            await self.send_dns_verify_report(context, chat_ids, result.new_ip, server_config=server_cfg)
            await self.send_auto_quality_report(context, chat_ids, server_config=server_cfg)

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

        sid = getattr(result, "server_id", "default")
        if failed_chat_ids:
            await persist_result_for_notification(result, chat_id=failed_chat_ids[0])
        else:
            mark_notification_sent(server_id=sid)

    async def try_send_pending_notifications(self, context: ContextTypes.DEFAULT_TYPE):
        pending_list = get_all_pending_notifications()
        if not pending_list:
            return

        for sid, pending in pending_list:
            if pending.get("sending_notify"):
                continue

            chat_id = pending.get("last_chat_id") or str(config.get("telegram_chat_id", "")).split(",")[0].strip()
            message = pending.get("last_message", "")
            if not chat_id or not message:
                continue

            try:
                mark_sending_notify(True, server_id=sid)
                await context.bot.send_message(chat_id=chat_id, text=message)
                mark_notification_sent(server_id=sid)
                logger.info(f"已补发【{sid}】上次未送达的换IP结果通知")
            except Exception as e:
                mark_sending_notify(False, server_id=sid)
                logger.warning(f"补发【{sid}】换IP结果通知失败，稍后重试: {e}")

    async def send_dns_verify_report(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_ids: list[str],
        target_ip: str,
        server_config: Optional[Dict[str, Any]] = None,
    ):
        cfg = server_config if server_config is not None else config
        sid = cfg.get("id", "default")
        sname = cfg.get("name", sid)
        if not cfg.get("dns_verify_enabled", True):
            return
        if not is_dns_update_enabled(server_config=cfg):
            return

        record_name = get_dns_record_name(server_config=cfg).strip().rstrip(".")
        if not record_name or not target_ip:
            return

        delay = int(cfg.get("dns_verify_delay_seconds", 60))
        retry_count = int(cfg.get("dns_verify_retry_count", 10))

        prefix = f"【{sname}】" if is_multi_server_mode() else ""
        for attempt in range(1, retry_count + 1):
            await asyncio.sleep(max(1, delay))
            try:
                records = await asyncio.to_thread(resolve_ipv4_records, record_name)
                if target_ip in records:
                    text = (
                        f"{prefix}DNS解析已生效\n"
                        f"域名: {record_name}\n"
                        f"目标IP: {target_ip}\n"
                        f"当前解析: {', '.join(records)}\n"
                        f"检查次数: {attempt}/{retry_count}"
                    )
                    for chat_id in chat_ids:
                        await context.bot.send_message(chat_id=chat_id, text=text)
                    return

                logger.info(
                    f"{prefix}DNS解析暂未生效: {record_name}, target={target_ip}, "
                    f"records={records}, attempt={attempt}/{retry_count}"
                )
            except Exception as e:
                records = []
                logger.warning(f"{prefix}DNS解析检查失败: {e}")

        text = (
            f"{prefix}DNS解析暂未确认生效\n"
            f"域名: {record_name}\n"
            f"目标IP: {target_ip}\n"
            f"最后解析: {', '.join(records) if records else '未获取到A记录'}\n"
            f"已检查: {retry_count} 次"
        )
        for chat_id in chat_ids:
            await context.bot.send_message(chat_id=chat_id, text=text)

    async def send_auto_quality_report(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_ids: list[str],
        server_config: Optional[Dict[str, Any]] = None,
    ):
        cfg = server_config if server_config is not None else config
        sid = cfg.get("id", "default")
        sname = cfg.get("name", sid)
        if not cfg.get("auto_change_quality_report", True):
            return
        if not cfg.get("ip_quality_enabled", True):
            return

        tmp_dir = None
        prefix = f"【{sname}】" if is_multi_server_mode() else ""
        try:
            quality_cmd = str(cfg.get("ip_quality_cmd") or "").strip()
            return_code, output = await asyncio.to_thread(run_quality_command, quality_cmd, server_config=cfg)
            logger.info(f"{prefix}自动IP质量检测命令返回码: {return_code}")

            svg_urls = extract_svg_urls(output)
            if not svg_urls:
                text = (
                    f"{prefix}自动IP质量检测完成，但没有识别到SVG链接。\n"
                    f"命令返回码: {return_code}\n\n"
                    f"最近输出:\n{redact_text((output or '无输出')[-1500:])}"
                )
                for chat_id in chat_ids:
                    await context.bot.send_message(chat_id=chat_id, text=text)
                return

            tmp_dir = tempfile.mkdtemp(prefix="auto_ip_quality_")
            successful_items = []
            failed_items = []
            total_reports = len(svg_urls)

            for idx, svg_url in enumerate(svg_urls):
                label = "IPv4" if idx == 0 else "IPv6" if idx == 1 else f"节点 {idx + 1}"
                png_path = str(Path(tmp_dir) / f"auto_report_{idx}.png")
                jpg_path = str(Path(tmp_dir) / f"auto_report_{idx}.jpg")
                try:
                    await asyncio.to_thread(render_svg_url_to_png, svg_url, png_path)
                    await asyncio.to_thread(crop_report_area, png_path, jpg_path)
                    successful_items.append({"label": label, "jpg_path": jpg_path, "url": svg_url})
                except Exception as render_err:
                    logger.warning(f"{prefix}自动IP质量图片渲染失败 ({label}): {render_err}")
                    failed_items.append({"label": label, "error": str(render_err), "url": svg_url})

            from telegram import InputMediaPhoto
            for chat_id in chat_ids:
                if len(successful_items) == 1:
                    item = successful_items[0]
                    with open(item["jpg_path"], "rb") as f:
                        await context.bot.send_photo(
                            chat_id=chat_id,
                            photo=f,
                            caption=f"{prefix}自动换IP后的【{item['label']}】IP质量检测报告\n🔗 报告链接: {item['url']}",
                        )
                elif len(successful_items) > 1:
                    files_to_close = []
                    try:
                        media = []
                        for item in successful_items:
                            f = open(item["jpg_path"], "rb")
                            files_to_close.append(f)
                            media.append(InputMediaPhoto(
                                media=f,
                                caption=f"{prefix}自动换IP后的【{item['label']}】IP质量检测报告\n🔗 报告链接: {item['url']}",
                            ))
                        await context.bot.send_media_group(chat_id=chat_id, media=media)
                    except Exception as mg_err:
                        logger.warning(f"{prefix}自动换IP报告媒体组发送失败，降级为单张发送: {mg_err}")
                        for item in successful_items:
                            with open(item["jpg_path"], "rb") as f:
                                await context.bot.send_photo(
                                    chat_id=chat_id,
                                    photo=f,
                                    caption=f"{prefix}自动换IP后的【{item['label']}】IP质量检测报告\n🔗 报告链接: {item['url']}",
                                )
                    finally:
                        for f in files_to_close:
                            try:
                                f.close()
                            except Exception:
                                pass

                for item in failed_items:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"{prefix}自动换IP成功，【{item['label']}】图片渲染失败，可直接点击查看报告：\n🔗 {item['url']}",
                    )
        except Exception as e:
            logger.exception(f"{prefix}自动IP质量检测失败: {e}")
            for chat_id in chat_ids:
                await context.bot.send_message(chat_id=chat_id, text=f"{prefix}自动IP质量检测失败：{redact_text(str(e))}")
        finally:
            if tmp_dir and os.path.isdir(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)

    async def post_init(self, application: Application):
        try:
            active_commands = [
                cmd for cmd in BOT_COMMANDS
                if is_multi_server_mode() or cmd.command not in ("servers", "use")
            ]
            await application.bot.set_my_commands(active_commands, scope=BotCommandScopeDefault())
            await application.bot.set_my_commands(active_commands, scope=BotCommandScopeAllPrivateChats())
            await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())

            target_chat_ids = set()
            for raw_val in [
                config.get("telegram_chat_id"),
                config.get("telegram_super_admin_user_ids"),
                config.get("telegram_admin_user_ids"),
                config.get("telegram_allowed_user_ids"),
            ]:
                for item in str(raw_val or "").split(","):
                    item = item.strip()
                    if item:
                        try:
                            target_chat_ids.add(int(item))
                        except ValueError:
                            pass

            for cid in target_chat_ids:
                try:
                    await application.bot.set_my_commands(active_commands, scope=BotCommandScopeChat(chat_id=cid))
                    await application.bot.set_chat_menu_button(chat_id=cid, menu_button=MenuButtonCommands())
                except Exception as ex:
                    logger.debug(f"设置单独聊天菜单失败 (chat_id={cid}): {ex}")

            logger.info("已注册 Telegram 机器人命令菜单与 MenuButton（含全局、私聊及管理员作用域）")
        except Exception as e:
            logger.warning(f"注册 Telegram 命令菜单失败: {e}")

        # 后台异步启动系统依赖自检与自愈，不阻塞机器人初始化响应
        try:
            asyncio.create_task(asyncio.to_thread(ensure_system_dependencies))
        except Exception as ex:
            logger.debug(f"启动系统依赖自检任务异常: {ex}")

    def get_auto_change_jobs(self):
        if not self.app or not self.app.job_queue:
            return []
        jobs = []
        for job in self.app.job_queue.jobs():
            if job.name and job.name.startswith(AUTO_CHANGE_JOB_NAME):
                jobs.append(job)
        return jobs

    def schedule_auto_change_job(self) -> bool:
        if not self.app or not self.app.job_queue:
            logger.warning("JobQueue 不可用，请确认安装了 python-telegram-bot[job-queue]")
            return False

        servers = get_servers()
        if not is_multi_server_mode():
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
                data={"server_id": servers[0].get("id", "default")},
            )
            logger.info(f"自动换IP任务已注册，每天北京时间 {run_time.strftime('%H:%M')} 执行一次")
            return True

        scheduled_count = 0
        for s in servers:
            sid = s.get("id", "default")
            sname = s.get("name") or sid
            s_enabled = s.get("auto_change_enabled", config.get("auto_change_enabled", True))
            if not s_enabled:
                logger.info(f"服务器【{sname}】({sid}) 未启用自动换IP，跳过注册")
                continue

            raw_time = str(s.get("auto_change_time") or config.get("auto_change_time", "04:00")).strip()
            try:
                run_time = parse_auto_change_time(raw_time)
            except ValueError as e:
                logger.warning(f"服务器【{sname}】({sid}) 自动换IP时间配置无效: {e}")
                continue

            job_name = f"{AUTO_CHANGE_JOB_NAME}_{sid}"
            existing = self.app.job_queue.get_jobs_by_name(job_name)
            if existing:
                logger.info(f"服务器【{sname}】({sid}) 定时任务已存在，跳过重复注册")
                continue

            self.app.job_queue.run_daily(
                self.auto_change_job,
                time=run_time,
                name=job_name,
                data={"server_id": sid},
            )
            logger.info(f"服务器【{sname}】({sid}) 自动换IP任务已注册，每天北京时间 {run_time.strftime('%H:%M')} 执行一次")
            scheduled_count += 1

        return scheduled_count > 0

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
            await update.message.reply_text("已写入启用配置，但未成功注册定时任务（请检查 JobQueue 或时间配置）。")

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
        lines = [
            "自动换IP状态",
            f"全局配置: {'已启用' if enabled else '已关闭'}",
            f"定时任务: {'运行中 (' + str(len(jobs)) + ' 个任务)' if jobs else '未注册'}",
            f"全局时间: 每天北京时间 {auto_time}",
            f"失败重试: 最多 {retry_count} 次，间隔 {retry_delay} 秒",
        ]
        if is_multi_server_mode():
            servers = get_servers()
            lines.append(f"\n【受管节点列表 (共 {len(servers)} 个)】")
            for s in servers:
                sid = s.get("id", "default")
                sname = s.get("name") or sid
                s_en = s.get("auto_change_enabled", enabled)
                s_time = s.get("auto_change_time", auto_time)
                lines.append(f"• {s_name} ({sid}): {'已启用' if s_en else '已关闭'} (时间: {s_time})")

        await update.message.reply_text("\n".join(lines))

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

        super_admin_ids = _get_super_admin_id_list()
        admin_ids = _get_admin_id_list()

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
        return INVISIBLE_MESSAGE_TEXT

    def user_management_keyboard(self, notice: str = "") -> InlineKeyboardMarkup:
        admin_ids = _get_admin_id_list()
        selected_admin_id = str(getattr(self, "_user_management_selected_admin_id", "") or "").strip()

        rows = [[InlineKeyboardButton("用户管理", callback_data="manage_users:noop")]]
        if notice:
            rows.append([InlineKeyboardButton(notice, callback_data="manage_users:noop")])

        rows.append([InlineKeyboardButton("普通管理员", callback_data="manage_users:noop")])
        if admin_ids:
            rows.extend([
                [
                    InlineKeyboardButton(
                        f"{'✅ ' if admin_id == selected_admin_id else ''}{admin_id}",
                        callback_data=f"manage_users:select:{admin_id}",
                    )
                    for admin_id in admin_ids[idx:idx + 2]
                ]
                for idx in range(0, len(admin_ids), 2)
            ])
        else:
            rows.append([InlineKeyboardButton("暂无普通管理员", callback_data="manage_users:noop")])

        rows.extend([
            [
                InlineKeyboardButton("添加普通管理员", callback_data="manage_users:add"),
                InlineKeyboardButton("删除选中", callback_data="manage_users:delete_selected"),
            ],
            [InlineKeyboardButton("刷新列表", callback_data="manage_users:menu")],
        ])
        return InlineKeyboardMarkup(rows)

    def add_admin_prompt_keyboard(self, notice: str = "") -> InlineKeyboardMarkup:
        rows = [[InlineKeyboardButton("添加普通管理员", callback_data="manage_users:noop")]]
        if notice:
            rows.append([InlineKeyboardButton(notice, callback_data="manage_users:noop")])
        rows.extend([
            [InlineKeyboardButton("请发送 Telegram USER_ID", callback_data="manage_users:noop")],
            [InlineKeyboardButton("返回用户管理", callback_data="manage_users:menu")],
        ])
        return InlineKeyboardMarkup(rows)

    async def manage_users(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_super_admin_permission(update):
            return

        context.user_data.pop("awaiting_add_admin", None)
        context.user_data.pop("selected_admin_id", None)
        self._user_management_selected_admin_id = ""
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

        if data == "manage_users:noop":
            return

        if data == "manage_users:menu":
            context.user_data.pop("awaiting_add_admin", None)
            self._user_management_selected_admin_id = str(context.user_data.get("selected_admin_id") or "").strip()
            await query.edit_message_text(
                self.user_management_text(),
                reply_markup=self.user_management_keyboard(),
            )
            return

        if data == "manage_users:add":
            context.user_data["awaiting_add_admin"] = True
            await query.edit_message_text(
                INVISIBLE_MESSAGE_TEXT,
                reply_markup=self.add_admin_prompt_keyboard("只接受 5-20 位数字"),
            )
            return

        if data.startswith("manage_users:select:"):
            selected_admin_id = data.split(":", 2)[2].strip()
            admin_ids = _get_admin_id_list()
            if selected_admin_id not in admin_ids:
                context.user_data.pop("selected_admin_id", None)
                self._user_management_selected_admin_id = ""
            else:
                context.user_data["selected_admin_id"] = selected_admin_id
                self._user_management_selected_admin_id = selected_admin_id
            await query.edit_message_text(
                self.user_management_text(),
                reply_markup=self.user_management_keyboard(),
            )
            return

        if data == "manage_users:delete_selected":
            admin_id = str(context.user_data.get("selected_admin_id") or "").strip()
            admin_ids = _get_admin_id_list()
            if not admin_id:
                await query.edit_message_text(
                    INVISIBLE_MESSAGE_TEXT,
                    reply_markup=self.user_management_keyboard("请先选择普通管理员"),
                )
                return
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

            context.user_data.pop("selected_admin_id", None)
            self._user_management_selected_admin_id = ""
            await query.edit_message_text(
                self.user_management_text(),
                reply_markup=self.user_management_keyboard("已删除普通管理员"),
            )
            return

    async def remove_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_super_admin_permission(update):
            return

        admin_ids = _get_admin_id_list()
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
        admin_ids = _get_admin_id_list()
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
            await update.message.reply_text(
                INVISIBLE_MESSAGE_TEXT,
                reply_markup=self.add_admin_prompt_keyboard("USER_ID 格式无效"),
            )
            return

        super_admin_ids = _get_super_admin_id_list()
        admin_ids = _get_admin_id_list()

        context.user_data.pop("awaiting_add_admin", None)
        context.user_data.pop("selected_admin_id", None)
        self._user_management_selected_admin_id = ""
        if new_admin_id in super_admin_ids:
            await update.message.reply_text(
                self.user_management_text(),
                reply_markup=self.user_management_keyboard("该用户已经是超级管理员"),
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
            self.user_management_text(),
            reply_markup=self.user_management_keyboard("已添加普通管理员"),
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

    async def ip_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_user_permission(update):
            return

        server_cfg, is_all, prompt_shown = await resolve_target_server(update, context, "ip_status", allow_all=True)
        if prompt_shown:
            return

        msg = update.message or (update.callback_query.message if update.callback_query else None)
        if not msg:
            return

        async def _format_single(cfg: Dict[str, Any]) -> str:
            sid = str(cfg.get("id") or "default").strip()
            sname = str(cfg.get("name") or sid).strip()
            provider = str(cfg.get("ip_change_provider", "generic")).strip().lower()
            if provider == "boil":
                mode_name = "Boil Network 住宅模式"
            elif provider in ("fachost", "classic"):
                mode_name = "Fachost 专用模式 (解析响应 JSON)"
            else:
                mode_name = "通用 API 模式 (curl -4 ip.sb 轮询探测)"
            title = f"【{sname} ({sid}) 换IP配置与状态】" if is_multi_server_mode() else "【换IP配置与状态】"
            lines = [title, f"- 换IP模式: {mode_name} ({provider})"]

            state = load_server_state(sid)
            if provider == "boil":
                token = str(cfg.get("boil_api_token", "")).strip()
                lines.append(f"- Boil Token: {'已配置' if token else '未配置'}")
                base_url = str(cfg.get("boil_api_base_url", "https://ippanel.boil.network")).strip()
                lines.append(f"- Boil 接口地址: {base_url}")
                if token:
                    try:
                        curr_boil_ip = await asyncio.to_thread(call_boil_get_ip, base_url, token, 10)
                        lines.append(f"- 当前住宅IP: {curr_boil_ip}")
                    except Exception as e:
                        lines.append(f"- 当前住宅IP: 获取失败 ({e})")

                uses_left = state.get("boil_uses_left", -1)
                lines.append(f"- 今日剩余配额: {uses_left if uses_left >= 0 else '暂无缓存（将在换IP后更新）'}")

                next_allowed_at = float(state.get("boil_next_allowed_at", 0) or 0)
                now = time.time()
                if now < next_allowed_at:
                    wait_sec = max(1, int(next_allowed_at - now))
                    next_time_str = dt.datetime.fromtimestamp(next_allowed_at).strftime("%H:%M:%S")
                    lines.append(f"- 冷却状态: 冷却中，还需等待 {wait_sec} 秒（预计可用: {next_time_str}）")
                else:
                    lines.append("- 冷却状态: 就绪（当前无限制）")
            else:
                api_url = str(cfg.get("ip_change_api", "")).strip()
                lines.append(f"- 换IP接口URL: {'已配置' if api_url else '未配置'}")
                interval = int(cfg.get("ip_change_interval", 2))
                lines.append(f"- 最小更换间隔: {interval} 分钟")

            last_time = get_last_change_time(server_id=sid)
            if last_time:
                last_dt = dt.datetime.fromtimestamp(last_time).strftime("%Y-%m-%d %H:%M:%S")
                lines.append(f"- 上次更换时间: {last_dt}")
                lines.append(f"- 上次更换结果: {state.get('last_change_status') or '无'}")
            return "\n".join(lines)

        if is_all:
            servers = get_servers()
            tasks = [_format_single(s) for s in servers]
            results = await asyncio.gather(*tasks)
            await msg.reply_text("🌐【所有服务器换IP配置与冷却状态】\n\n" + "\n\n".join(results))
        else:
            res_text = await _format_single(server_cfg)
            hint = ""
            if is_multi_server_mode():
                sname = server_cfg.get("name", server_cfg.get("id"))
                hint = f"\n\n💡 提示: 当前操作服务器为 [{sname}]，输入 /servers 查看列表，/use 切换"
            await msg.reply_text(res_text + hint)

    async def set_ip_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_super_admin_permission(update):
            return

        if context.args:
            target = context.args[0].strip().lower()
            if target == "classic":
                target = "fachost"
            if target not in ("generic", "fachost", "boil"):
                await update.message.reply_text("无效的模式，支持: generic (通用模式), fachost (Fachost专用), boil (Boil Network)")
                return

            try:
                config["ip_change_provider"] = target
                persist_config_value("ip_change_provider", target)
                if target == "boil":
                    mode_desc = "Boil Network 住宅模式"
                elif target == "fachost":
                    mode_desc = "Fachost 专用模式 (解析响应 JSON)"
                else:
                    mode_desc = "通用 API 模式 (curl -4 ip.sb 轮询探测)"
                await update.message.reply_text(f"已切换换IP模式为: {mode_desc} ({target})")
            except Exception as e:
                logger.exception(f"切换换IP模式失败: {e}")
                await update.message.reply_text(f"切换换IP模式失败: {redact_text(str(e))}")
            return

        current = str(config.get("ip_change_provider", "generic")).strip().lower()
        if current == "classic":
            current = "fachost"
        keyboard = [
            [
                InlineKeyboardButton(
                    f"{'✅ ' if current == 'generic' else ''}通用 API 模式 (generic - 推荐，curl -4 ip.sb 轮询)",
                    callback_data="set_ip_mode:generic",
                )
            ],
            [
                InlineKeyboardButton(
                    f"{'✅ ' if current in ('fachost', 'classic') else ''}Fachost 专用模式 (fachost - 解析响应 JSON)",
                    callback_data="set_ip_mode:fachost",
                )
            ],
            [
                InlineKeyboardButton(
                    f"{'✅ ' if current == 'boil' else ''}Boil Network 住宅模式 (boil - 官方云端 API)",
                    callback_data="set_ip_mode:boil",
                )
            ],
        ]
        await update.message.reply_text(
            f"请选择换IP服务商模式（当前: {current}）：",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def set_ip_mode_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_super_admin_permission(update):
            return

        query = update.callback_query
        await query.answer()

        target = (query.data or "").split(":", 1)[1].strip().lower()
        if target == "classic":
            target = "fachost"
        if target not in ("generic", "fachost", "boil"):
            return

        try:
            config["ip_change_provider"] = target
            persist_config_value("ip_change_provider", target)
            if target == "boil":
                mode_desc = "Boil Network 住宅模式"
            elif target == "fachost":
                mode_desc = "Fachost 专用模式 (解析响应 JSON)"
            else:
                mode_desc = "通用 API 模式 (curl -4 ip.sb 轮询探测)"
            await query.edit_message_text(f"已成功切换换IP模式为: {mode_desc} ({target})")
        except Exception as e:
            logger.exception(f"切换换IP模式失败: {e}")
            await query.edit_message_text(f"切换换IP模式失败: {redact_text(str(e))}")

    async def set_boil_token(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_super_admin_permission(update):
            return

        if not context.args:
            await update.message.reply_text("用法: /set_boil_token TOKEN\n例如: /set_boil_token your_token_here")
            return

        token = context.args[0].strip()
        try:
            await update.message.delete()
        except Exception:
            pass

        try:
            config["boil_api_token"] = token
            persist_config_value("boil_api_token", token)
            await update.effective_chat.send_message("已成功设置并保存 Boil API Token。")
        except Exception as e:
            logger.exception(f"保存 Boil Token 失败: {e}")
            await update.effective_chat.send_message(f"保存 Boil Token 失败: {redact_text(str(e))}")

    async def set_ip_api(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_super_admin_permission(update):
            return

        if not context.args:
            await update.message.reply_text("用法: /set_ip_api URL\n例如: /set_ip_api https://example.com/change-ip")
            return

        url = context.args[0].strip()
        try:
            config["ip_change_api"] = url
            persist_config_value("ip_change_api", url)
            await update.message.reply_text("已成功设置并保存经典模式换IP接口 URL。")
        except Exception as e:
            logger.exception(f"保存 IP API URL 失败: {e}")
            await update.message.reply_text(f"保存 IP API URL 失败: {redact_text(str(e))}")

    async def servers_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_user_permission(update):
            return

        user_id = update.effective_user.id if update.effective_user else 0
        servers = get_servers()
        selected_sid = get_user_selected_server(user_id) or servers[0].get("id", "default")

        lines = [f"🌐【服务器节点看板】(共 {len(servers)} 个节点)\n"]
        for idx, s in enumerate(servers, 1):
            sid = s.get("id", "default")
            sname = s.get("name") or sid
            provider = str(s.get("ip_change_provider") or "generic").strip().lower()
            if provider == "boil":
                mode_str = "Boil Network 住宅模式"
            elif provider in ("fachost", "classic"):
                mode_str = "Fachost 专用模式"
            else:
                mode_str = "通用 API 模式"

            ssh_enabled = bool(s.get("remote_ssh_enabled"))
            ssh_host = str(s.get("remote_ssh_host") or "").strip()
            ssh_str = f"已启用 ({ssh_host})" if ssh_enabled else "未启用 (本地机房)"

            dns_on = bool(s.get("dns_update_enabled"))
            dns_rec = str(s.get("dns_record_name") or s.get("huawei_dns_record_name", "")).strip()
            dns_str = f"已启用 ({dns_rec})" if (dns_on and dns_rec) else "未启用"

            s_state = load_server_state(sid)
            last_change = s_state.get("last_change_time")
            last_dt_str = dt.datetime.fromtimestamp(last_change).strftime("%m-%d %H:%M") if last_change else "无记录"
            last_st = s_state.get("last_change_status") or "未知"

            current_mark = " 👉 [当前默认]" if sid == selected_sid else ""
            lines.append(
                f"{idx}. 🏷️ 【{sname}】 (ID: `{sid}`){current_mark}\n"
                f"   • 换IP模式: {mode_str}\n"
                f"   • 远程SSH: {ssh_str}\n"
                f"   • DNS同步: {dns_str}\n"
                f"   • 上次换IP: {last_dt_str} ({last_st})"
            )

        keyboard = []
        if len(servers) > 1:
            switch_btns = []
            for s in servers:
                sid = s.get("id", "default")
                sname = s.get("name") or sid
                label = f"设为默认: {sname}" if sid != selected_sid else f"✅ {sname}"
                switch_btns.append(InlineKeyboardButton(label, callback_data=f"srv_act:use:{sid}"))
                if len(switch_btns) == 2:
                    keyboard.append(switch_btns)
                    switch_btns = []
            if switch_btns:
                keyboard.append(switch_btns)

            keyboard.append([
                InlineKeyboardButton("🌐 全部检测 (/check all)", callback_data="srv_act:check:all"),
                InlineKeyboardButton("🔄 全部换IP (/change all)", callback_data="srv_act:change:all"),
            ])

        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
        msg = update.message or (update.callback_query.message if update.callback_query else None)
        if msg:
            await msg.reply_text("\n\n".join(lines), reply_markup=reply_markup, parse_mode="Markdown")

    async def use_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_user_permission(update):
            return

        user_id = update.effective_user.id if update.effective_user else 0
        servers = get_servers()

        msg = update.message or (update.callback_query.message if update.callback_query else None)
        if not msg:
            return

        if context.args:
            target = context.args[0].strip().lower()
            matched = get_server_config(target)
            if not matched:
                avail = ", ".join([f"`{s['id']}`" for s in servers])
                await msg.reply_text(f"❌ 未找到 ID 为 `{target}` 的服务器。\n可用服务器 ID: {avail}", parse_mode="Markdown")
                return
            set_user_selected_server(user_id, matched["id"])
            await msg.reply_text(f"✅ 已将当前默认操作服务器切换为: 【{matched.get('name', matched['id'])}】 (`{matched['id']}`)", parse_mode="Markdown")
            return

        keyboard = []
        row = []
        current_sid = get_user_selected_server(user_id) or servers[0].get("id", "default")
        for s in servers:
            sid = s.get("id", "default")
            sname = s.get("name") or sid
            prefix = "✅ " if sid == current_sid else ""
            row.append(InlineKeyboardButton(f"{prefix}{sname}", callback_data=f"srv_act:use:{sid}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)

        await msg.reply_text("请选择要切换的默认操作服务器：", reply_markup=InlineKeyboardMarkup(keyboard))

    async def server_action_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_user_permission(update):
            return

        query = update.callback_query
        await query.answer()
        data = query.data or ""
        parts = data.split(":", 2)
        if len(parts) < 3:
            return

        cmd = parts[1].strip()
        target = parts[2].strip()
        user_id = update.effective_user.id if update.effective_user else 0

        if cmd == "cancel":
            await query.edit_message_text("已取消操作。")
            return

        if cmd == "use":
            matched = get_server_config(target)
            if matched:
                set_user_selected_server(user_id, matched["id"])
                sname = matched.get("name", matched["id"])
                await query.edit_message_text(f"✅ 已将默认操作服务器切换为: 【{sname}】 (`{matched['id']}`)", parse_mode="Markdown")
            else:
                await query.edit_message_text(f"❌ 切换失败，未找到节点: {target}")
            return

        context.args = [target]
        if cmd == "check":
            await check_ip_status(update, context)
        elif cmd == "change":
            await change_ip_handler(update, context)
        elif cmd == "quality":
            await ip_quality_handler(update, context)
        elif cmd == "stream":
            await stream_check_handler(update, context)
        elif cmd == "speedtest":
            await speedtest_handler(update, context)
        elif cmd == "ping":
            await ping_handler(update, context)
        elif cmd == "ip_status":
            await self.ip_status(update, context)

    async def health(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_user_permission(update):
            return

        checks = []
        checks.append(f"自动换IP: {'已启用' if config.get('auto_change_enabled') else '已关闭'}")
        checks.append(f"自动时间: 每天北京时间 {config.get('auto_change_time', '04:00')}")
        checks.append(
            "自动重试: "
            f"最多 {int(config.get('auto_change_retry_count', 5))} 次，"
            f"间隔 {int(config.get('auto_change_retry_delay_seconds', 60))} 秒"
        )

        state_file = Path(str(config.get("state_file", "/var/lib/vps-ip-bot/state.json"))).expanduser()
        state_parent = state_file.parent
        checks.append(f"状态文件目录: {'可写' if os.access(state_parent, os.W_OK) else '不可写'} ({state_parent})")

        dep_status = get_system_dependency_status()
        cairo_info = dep_status["cairo"]["detail"]
        cairo_icon = "✅" if dep_status["cairo"]["ok"] else "⚠️"
        checks.append(f"IP质量图片渲染: {cairo_icon} {cairo_info}")

        font_info = dep_status["font"]["detail"]
        font_icon = "✅" if dep_status["font"]["ok"] else "⚠️"
        checks.append(f"中文字体环境: {font_icon} {font_info}")

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

        servers = get_servers()
        from utils.remote_ssh import is_remote_ssh_enabled, test_remote_ssh_connectivity, get_ssh_config
        if not is_multi_server_mode():
            s = servers[0]
            try:
                current_ip = await asyncio.to_thread(get_current_ip)
                checks.append(f"公网IP: {current_ip}")
            except Exception as e:
                checks.append(f"公网IP: 获取失败 ({e})")

            ip_provider = str(s.get("ip_change_provider", "generic")).strip().lower()
            if ip_provider == "boil":
                token_set = bool(str(s.get("boil_api_token", "")).strip())
                checks.append(f"换IP模式: Boil Network ({'Token已配置' if token_set else 'Token未配置'})")
            else:
                api_set = bool(str(s.get("ip_change_api", "")).strip())
                checks.append(f"换IP模式: 接口模式 ({'API已配置' if api_set else 'API未配置'})")

            dns_provider = get_dns_provider_name(server_config=s) or "未配置"
            checks.append(f"DNS更新: {'已启用' if is_dns_update_enabled(server_config=s) else '未启用'} ({dns_provider})")

            record_name = get_dns_record_name(server_config=s).strip()
            if is_dns_update_enabled(server_config=s) and record_name:
                try:
                    records = await asyncio.to_thread(resolve_ipv4_records, record_name)
                    checks.append(f"DNS解析: {record_name} -> {', '.join(records) if records else '无A记录'}")
                except Exception as e:
                    checks.append(f"DNS解析: 检查失败 ({e})")

            if is_remote_ssh_enabled(s):
                ssh_c = get_ssh_config(server_config=s)
                try:
                    ok, target_ip, rtt = await asyncio.to_thread(test_remote_ssh_connectivity, server_config=s)
                    if ok:
                        checks.append(f"家宽SSH连通: ✅ 正常 (目标: {target_ip}:{ssh_c['port']}, 延迟: {rtt}ms)")
                    else:
                        checks.append(f"家宽SSH连通: ⚠️ 无法连通 (目标: {target_ip}:{ssh_c['port']})")
                except Exception as ex:
                    checks.append(f"家宽SSH连通: ⚠️ 检测异常 ({ex})")
            else:
                checks.append("家宽SSH连通: 未启用 (使用机房本地环境运行)")
        else:
            checks.append(f"\n【受管服务器看板 (共 {len(servers)} 台)】")
            for idx, s in enumerate(servers, 1):
                sid = s.get("id", "default")
                sname = s.get("name") or sid
                provider = s.get("ip_change_provider", "generic")
                node_lines = [f"{idx}. 🏷️ 【{sname}】 (`{sid}`): 模式={provider}"]
                if is_remote_ssh_enabled(s):
                    ssh_c = get_ssh_config(server_config=s)
                    try:
                        ok, target_ip, rtt = await asyncio.to_thread(test_remote_ssh_connectivity, server_config=s)
                        if ok:
                            node_lines.append(f"   • SSH连通: ✅ 正常 ({target_ip}:{ssh_c['port']}, {rtt}ms)")
                        else:
                            node_lines.append(f"   • SSH连通: ⚠️ 无法连通 ({target_ip}:{ssh_c['port']})")
                    except Exception as ex:
                        node_lines.append(f"   • SSH连通: ⚠️ 异常 ({ex})")
                else:
                    node_lines.append("   • SSH连通: 未启用 (本地机房)")

                if is_dns_update_enabled(server_config=s):
                    rec = get_dns_record_name(server_config=s)
                    node_lines.append(f"   • DNS更新: 已启用 ({rec})")
                else:
                    node_lines.append("   • DNS更新: 未启用")
                checks.append("\n".join(node_lines))

        msg = update.message or (update.callback_query.message if update.callback_query else None)
        if msg:
            await msg.reply_text("健康检查\n" + "\n".join(f"- {item}" if not item.startswith("\n") and not item.startswith("1") and not item.startswith("2") and not item.startswith("3") and not item.startswith("4") and not item.startswith("5") and not item.startswith("6") and not item.startswith("7") and not item.startswith("8") and not item.startswith("9") else item for item in checks))

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
        self.app.add_handler(CommandHandler("servers", self.servers_handler))
        self.app.add_handler(CommandHandler("nodes", self.servers_handler))
        self.app.add_handler(CommandHandler("use", self.use_handler))
        self.app.add_handler(CommandHandler("check", check_ip_status))
        self.app.add_handler(CommandHandler("change", change_ip_handler))
        self.app.add_handler(CommandHandler("ip_status", self.ip_status))
        self.app.add_handler(CommandHandler("set_ip_mode", self.set_ip_mode))
        self.app.add_handler(CommandHandler("set_boil_token", self.set_boil_token))
        self.app.add_handler(CommandHandler("set_ip_api", self.set_ip_api))
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
        self.app.add_handler(CallbackQueryHandler(self.server_action_callback, pattern="^srv_act:"))
        self.app.add_handler(CallbackQueryHandler(self.set_ip_mode_callback, pattern="^set_ip_mode:"))
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
