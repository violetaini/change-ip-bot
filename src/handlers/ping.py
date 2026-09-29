import asyncio
import os
import re
import subprocess
from typing import Tuple

from telegram import Update
from telegram.ext import ContextTypes

from config import config, is_multi_server_mode
from handlers.server_selection import resolve_target_server
from handlers.user_check import check_user_permission
from utils.logger import logger


def parse_ping_params(
    args: list[str],
    default_v4: str = "1.1.1.1",
    default_v6: str = "2606:4700:4700::1111",
    default_count: int = 10,
) -> Tuple[str, int, int, str]:
    """
    解析 ping 命令参数。
    支持:
      /ping                  -> 默认 v4 目标, count=10, ip_version=4
      /ping -6               -> 默认 v6 目标, count=10, ip_version=6
      /ping -4               -> 默认 v4 目标, count=10, ip_version=4
      /ping -6 2400:3200::1  -> 目标 2400:3200::1, count=10, ip_version=6
      /ping 2400:3200::1     -> 自动识别包含 ':' 为 IPv6, ip_version=6
      /ping -c 5 -6          -> count=5, 默认 v6 目标, ip_version=6
      /ping -c 5 8.8.8.8     -> count=5, 目标 8.8.8.8, ip_version=4

    返回: (target, count, ip_version, warning_message)
    """
    count = default_count
    warning = ""
    ip_version = None
    target = None

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "-c":
            if i + 1 < len(args) and args[i + 1].isdigit():
                count = int(args[i + 1])
                i += 2
            else:
                warning = "无效的 -c 参数，使用默认值"
                i += 1
        elif arg == "-6":
            ip_version = 6
            i += 1
        elif arg == "-4":
            ip_version = 4
            i += 1
        else:
            target = arg
            i += 1

    if count < 1:
        count = 1
    elif count > 100:
        count = 100
        warning = "Ping 次数已限制为最大值 100"

    if target:
        if ip_version is None:
            if ":" in target:
                ip_version = 6
            else:
                ip_version = 4
    else:
        if ip_version == 6:
            target = default_v6
        else:
            ip_version = 4
            target = default_v4

    return target, count, ip_version, warning


def format_ping_result(target: str, ip_version: int, output: str) -> str:
    stats_match = re.search(
        r'(\d+)\s+packets transmitted,\s+(\d+)\s+(?:packets\s+)?received,\s+(\d+)%\s+packet loss',
        output,
    )
    rtt_match = re.search(
        r'(?:rtt|round-trip)\s+min/avg/max/(?:mdev|stddev)\s*=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)',
        output,
    )

    if stats_match and rtt_match:
        transmitted, received, loss = stats_match.groups()
        min_rtt, avg_rtt, max_rtt, mdev = rtt_match.groups()
        return (
            f"Ping 结果 ({target} [IPv{ip_version}]):\n\n"
            f"📊 统计信息:\n"
            f"• 发送: {transmitted}\n"
            f"• 接收: {received}\n"
            f"• 丢包率: {loss}%\n\n"
            f"⏱️ 延迟:\n"
            f"• 最小: {min_rtt} ms\n"
            f"• 平均: {avg_rtt} ms\n"
            f"• 最大: {max_rtt} ms\n"
            f"• 抖动: {mdev} ms"
        )

    win_stats = re.search(
        r'Packets:\s+Sent\s*=\s*(\d+),\s*Received\s*=\s*(\d+),\s*Lost\s*=\s*(\d+)\s*\(([\d.]+)%\s*loss\)',
        output,
    )
    win_rtt = re.search(
        r'Minimum\s*=\s*(\d+)ms,\s*Maximum\s*=\s*(\d+)ms,\s*Average\s*=\s*(\d+)ms',
        output,
    )
    if win_stats and win_rtt:
        sent, rec, lost, loss = win_stats.groups()
        min_rtt, max_rtt, avg_rtt = win_rtt.groups()
        return (
            f"Ping 结果 ({target} [IPv{ip_version}]):\n\n"
            f"📊 统计信息:\n"
            f"• 发送: {sent}\n"
            f"• 接收: {rec}\n"
            f"• 丢包率: {loss}%\n\n"
            f"⏱️ 延迟:\n"
            f"• 最小: {min_rtt} ms\n"
            f"• 平均: {avg_rtt} ms\n"
            f"• 最大: {max_rtt} ms"
        )

    return output.strip() or "Ping 未返回可解析结果"


async def ping_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_permission(update):
        return

    user_id = update.effective_user.id if update.effective_user else 0
    logger.info(f"收到 ping 命令，用户ID: {user_id}")

    server_cfg, is_all, prompt_shown = await resolve_target_server(update, context, "ping", allow_all=False)
    if prompt_shown:
        return

    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if not msg:
        return

    cfg = server_cfg if server_cfg is not None else config
    sid = cfg.get("id", "default")
    sname = cfg.get("name", sid)

    default_v4 = str(cfg.get("ping_target", "1.1.1.1")).strip() or "1.1.1.1"
    default_v6 = str(cfg.get("ping_target_v6", "2606:4700:4700::1111")).strip() or "2606:4700:4700::1111"
    default_count = int(cfg.get("ping_count", 10))

    args = list(context.args) if context.args else []
    target, count, ip_version, warning = parse_ping_params(
        args, default_v4=default_v4, default_v6=default_v6, default_count=default_count
    )

    if warning:
        await msg.reply_text(warning)

    try:
        from utils.remote_ssh import get_ssh_config, is_remote_ssh_enabled, run_remote_ssh_command
        prefix = f"【{sname}】" if is_multi_server_mode() else ""
        if is_remote_ssh_enabled(server_cfg):
            ssh_cfg = get_ssh_config(server_config=server_cfg)
            await msg.reply_text(
                f"正在通过{prefix}远程家宽 SSH ({ssh_cfg['host']}) ping {target} (IPv{ip_version}, {count} 次)..."
            )
            ping_cmd_str = f"ping -{ip_version} -c {count} {target}"
            code, output = await asyncio.to_thread(run_remote_ssh_command, ping_cmd_str, timeout=300, server_config=server_cfg)
        else:
            await msg.reply_text(f"正在{prefix}ping {target} (IPv{ip_version}, {count} 次)...")
            ping_cmd = (
                ["ping", f"-{ip_version}", "-n", str(count), target]
                if os.name == "nt"
                else ["ping", f"-{ip_version}", "-c", str(count), target]
            )
            result = await asyncio.to_thread(
                subprocess.run,
                ping_cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )
            output = f"{result.stdout}\n{result.stderr}"

        prefix = f"【{sname}】" if is_multi_server_mode() else ""
        message = prefix + format_ping_result(target, ip_version, output)
        await msg.reply_text(message)
    except subprocess.TimeoutExpired:
        await msg.reply_text("Ping 超时")
    except Exception as e:
        await msg.reply_text(f"执行 ping 时出错: {str(e)}")

