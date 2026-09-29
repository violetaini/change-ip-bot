import asyncio
import json
import subprocess
from typing import Any, Dict, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import get_server_config, is_multi_server_mode
from handlers.server_selection import resolve_target_server
from handlers.user_check import check_user_permission
from utils.logger import logger


async def speedtest_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_permission(update):
        return

    user_id = update.effective_user.id if update.effective_user else 0
    logger.info(f"收到 speedtest 命令，用户ID: {user_id}")

    server_cfg, is_all, prompt_shown = await resolve_target_server(update, context, "speedtest", allow_all=False)
    if prompt_shown:
        return

    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if not msg:
        return

    sid = server_cfg.get("id", "default")
    sname = server_cfg.get("name", sid)
    if context.user_data is not None:
        context.user_data["speedtest_target_server_id"] = sid

    from utils.remote_ssh import get_ssh_config, is_remote_ssh_enabled, run_remote_ssh_command

    if is_remote_ssh_enabled(server_cfg):
        cfg = get_ssh_config(server_config=server_cfg)
        status_msg = await msg.reply_text(f"正在通过【{sname}】远程 SSH ({cfg['host']}) 获取测速节点列表...")
        cmd_str = "speedtest -L --accept-license --accept-gdpr --format=json"
        try:
            code, output = await asyncio.to_thread(run_remote_ssh_command, cmd_str, timeout=30, server_config=server_cfg)
            if "Limit reached" in output:
                await status_msg.edit_text("测速超过次数限制，请稍后再试")
                return
            servers = json.loads(output)["servers"]
            keyboard = []
            for server in servers[:20]:
                keyboard.append([
                    InlineKeyboardButton(
                        f"{server['name']} - {server['location']} - {server['country']}",
                        callback_data=f"speedtest_{server['id']}",
                    )
                ])
            keyboard.insert(0, [InlineKeyboardButton("自动选择最佳节点", callback_data="speedtest_auto")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            await status_msg.edit_text(f"【{sname}】请选择测速节点:", reply_markup=reply_markup)
        except Exception as e:
            await status_msg.edit_text(f"【{sname}】获取测速节点失败: {str(e)}")
        return

    status_msg = await msg.reply_text(f"正在获取【{sname}】测速节点列表...")
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["speedtest", "-L", "--accept-license", "--accept-gdpr", "--format=json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if "Limit reached" in result.stderr:
            await status_msg.edit_text("测速超过次数限制，请稍后再试")
            return
        servers = json.loads(result.stdout)["servers"]

        keyboard = []
        for server in servers[:20]:
            keyboard.append([
                InlineKeyboardButton(
                    f"{server['name']} - {server['location']} - {server['country']}",
                    callback_data=f"speedtest_{server['id']}",
                )
            ])
        keyboard.insert(0, [InlineKeyboardButton("自动选择最佳节点", callback_data="speedtest_auto")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await status_msg.edit_text(f"【{sname}】请选择测速节点:", reply_markup=reply_markup)
    except Exception as e:
        await status_msg.edit_text(f"【{sname}】获取测速节点失败: {str(e)}")


def format_speedtest_result(data: dict, server_name: str = "") -> str:
    """
    格式化 Ookla Speedtest JSON 结果，展示节点、客户端出口IP及栈类型（IPv4/IPv6）、上下行速率、延迟与链接。
    """
    server = data.get("server", {}) or {}
    s_name = server.get("name", "未知节点")
    server_loc = server.get("location", "")
    server_country = server.get("country", "")
    if server_loc and server_country:
        server_info = f"{s_name} ({server_loc}, {server_country})"
    elif server_loc or server_country:
        server_info = f"{s_name} ({server_loc or server_country})"
    else:
        server_info = s_name

    download_bw = data.get("download", {}).get("bandwidth", 0) or 0
    upload_bw = data.get("upload", {}).get("bandwidth", 0) or 0
    download_mbps = download_bw / 125000
    upload_mbps = upload_bw / 125000

    latency = data.get("ping", {}).get("latency", 0.0) or 0.0
    result_url = data.get("result", {}).get("url", "N/A") or "N/A"

    external_ip = str(data.get("interface", {}).get("externalIp", "") or "").strip()
    ip_line = ""
    if external_ip:
        stack_type = "IPv6" if ":" in external_ip else "IPv4"
        ip_line = f"🌐 客户端出口: {external_ip} ({stack_type})\n"

    prefix = f"【{server_name}】" if server_name else ""
    return (
        f"{prefix}测速结果:\n"
        f"测速节点: {server_info}\n"
        f"{ip_line}"
        f"⬇️ 下载速度: {download_mbps:.2f} Mbps\n"
        f"⬆️ 上传速度: {upload_mbps:.2f} Mbps\n"
        f"延迟: {latency:.2f} ms\n"
        f"结果链接: {result_url}"
    )


async def speedtest_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_permission(update):
        return

    query = update.callback_query
    await query.answer()

    if not query.data.startswith("speedtest_"):
        return

    ookla_server_id = query.data.split("_")[1]
    cmd = ["speedtest", "--accept-license", "--accept-gdpr", "--format=json"]
    if ookla_server_id != "auto":
        cmd.extend(["-s", ookla_server_id])

    target_sid = context.user_data.get("speedtest_target_server_id") if context.user_data else None
    server_cfg = get_server_config(target_sid)
    sname = server_cfg.get("name", server_cfg.get("id", "默认服务器")) if server_cfg else ""

    from utils.remote_ssh import get_ssh_config, is_remote_ssh_enabled, run_remote_ssh_command

    try:
        if is_remote_ssh_enabled(server_cfg):
            cfg = get_ssh_config(server_config=server_cfg)
            await query.edit_message_text(f"正在通过【{sname}】远程 SSH ({cfg['host']}) 进行测速...\n这可能需要 1~2 分钟...")
            cmd_str = f"speedtest {'-s ' + ookla_server_id if ookla_server_id != 'auto' else ''} --accept-license --accept-gdpr --format=json"
            code, output = await asyncio.to_thread(run_remote_ssh_command, cmd_str, timeout=600, server_config=server_cfg)
            try:
                data = json.loads(output)
            except json.JSONDecodeError:
                await query.edit_message_text(f"【{sname}】测速结果解析失败。原始输出：\n{output[:3000]}")
                return
        else:
            await query.edit_message_text(f"正在进行【{sname}】测速...\n这可能需要几分钟时间...")
            result = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                text=True,
                timeout=600,
            )
            try:
                data = json.loads(result.stdout)
            except json.JSONDecodeError:
                await query.edit_message_text(f"【{sname}】测速结果解析失败。原始输出：\n{result.stdout[:3000]}")
                return

        message = format_speedtest_result(data, server_name=sname if is_multi_server_mode() else "")
        await query.edit_message_text(message)
    except subprocess.TimeoutExpired:
        await query.edit_message_text("测速超时，请稍后重试")
    except Exception as e:
        logger.error(f"测速失败: {str(e)}")
        await query.edit_message_text(f"测速失败: {str(e)}")
