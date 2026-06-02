import asyncio
import json
import subprocess

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from handlers.user_check import check_user_permission
from utils.logger import logger


async def speedtest_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_permission(update):
        return

    user_id = update.effective_user.id
    user_name = update.effective_user.username
    full_name = update.effective_user.full_name
    logger.info(f"收到 speedtest 命令，用户ID: {user_id}，用户名: {user_name}，全名: {full_name}")

    await update.message.reply_text("正在获取测速节点列表...")
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ['speedtest', '-L', '--accept-license', '--accept-gdpr', '--format=json'],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if "Limit reached" in result.stderr:
            await update.message.reply_text("测速超过次数限制，请稍后再试")
            return
        servers = json.loads(result.stdout)['servers']

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
        await update.message.reply_text("请选择测速节点:", reply_markup=reply_markup)
    except Exception as e:
        await update.message.reply_text(f"获取测速节点失败: {str(e)}")


async def speedtest_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_permission(update):
        return

    query = update.callback_query
    await query.answer()

    if not query.data.startswith("speedtest_"):
        return

    server_id = query.data.split("_")[1]
    cmd = ['speedtest', '--accept-license', '--accept-gdpr', '--format=json']
    if server_id != 'auto':
        cmd.extend(['-s', server_id])

    await query.edit_message_text("正在进行测速...\n这可能需要几分钟时间...")

    try:
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
            await query.edit_message_text(f"测速结果解析失败。原始输出：\n{result.stdout[:3000]}")
            return

        message = (
            "测速结果:\n"
            f"测速节点: {data['server']['name']} ({data['server']['location']}, {data['server']['country']})\n"
            f"⬇️ 下载速度: {data['download']['bandwidth'] / 125000:.2f} Mbps\n"
            f"⬆️ 上传速度: {data['upload']['bandwidth'] / 125000:.2f} Mbps\n"
            f"延迟: {data['ping']['latency']:.2f} ms\n"
            f"结果链接: {data.get('result', {}).get('url', 'N/A')}"
        )
        await query.edit_message_text(message)
    except subprocess.TimeoutExpired:
        await query.edit_message_text("测速超时，请稍后重试")
    except Exception as e:
        logger.error(f"测速失败: {str(e)}")
        await query.edit_message_text(f"测速失败: {str(e)}")
