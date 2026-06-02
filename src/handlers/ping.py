import asyncio
import os
import re
import subprocess

from telegram import Update
from telegram.ext import ContextTypes

from config import config
from handlers.user_check import check_user_permission
from utils.logger import logger


async def ping_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_permission(update):
        return

    user_id = update.effective_user.id
    user_name = update.effective_user.username
    full_name = update.effective_user.full_name
    logger.info(f"收到 ping 命令，用户ID: {user_id}，用户名: {user_name}，全名: {full_name}")

    target = config.get('ping_target', '1.1.1.1')
    count = config.get('ping_count', 10)

    if context.args:
        args = context.args
        i = 0
        while i < len(args):
            if args[i] == '-c':
                if i + 1 < len(args) and args[i + 1].isdigit():
                    count = int(args[i + 1])
                    i += 2
                else:
                    await update.message.reply_text("无效的 -c 参数，使用默认值")
                    i += 1
            else:
                target = args[i]
                i += 1

    if count < 1:
        count = 1
    elif count > 100:
        count = 100
        await update.message.reply_text("Ping 次数已限制为最大值 100")

    await update.message.reply_text(f"正在 ping {target} ({count} 次)...")

    try:
        ping_cmd = ['ping', '-n', str(count), target] if os.name == 'nt' else ['ping', '-c', str(count), target]
        result = await asyncio.to_thread(
            subprocess.run,
            ping_cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
        output = result.stdout
        stats_match = re.search(r'(\d+) packets transmitted, (\d+) received, (\d+)% packet loss', output)
        rtt_match = re.search(r'min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)', output)

        if stats_match and rtt_match:
            transmitted, received, loss = stats_match.groups()
            min_rtt, avg_rtt, max_rtt, mdev = rtt_match.groups()
            message = (
                f"Ping 结果 ({target}):\n\n"
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
        else:
            message = output or result.stderr or "Ping 未返回可解析结果"

        await update.message.reply_text(message)
    except subprocess.TimeoutExpired:
        await update.message.reply_text("Ping 超时")
    except Exception as e:
        await update.message.reply_text(f"执行 ping 时出错: {str(e)}")
