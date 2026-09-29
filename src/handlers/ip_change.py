import asyncio
from typing import Any, Dict, Optional

from telegram import Update
from telegram.ext import ContextTypes

from config import get_servers, is_multi_server_mode
from handlers.server_selection import resolve_target_server
from handlers.user_check import check_user_permission
from services.ip_change_service import (
    ChangeResult,
    build_result_message,
    perform_ip_change,
    persist_result_for_notification,
)
from utils.logger import logger
from utils.state import mark_notification_sent, mark_sending_notify


async def do_change_single_server(server_cfg: Optional[Dict[str, Any]], chat_id: str = "") -> ChangeResult:
    result = await perform_ip_change(trigger="manual", server_config=server_cfg)
    await persist_result_for_notification(result, chat_id=chat_id)
    return result


async def change_ip_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_permission(update):
        return

    user_id = update.effective_user.id if update.effective_user else 0
    logger.info(f"收到 change 命令，用户ID: {user_id}")

    server_cfg, is_all, prompt_shown = await resolve_target_server(update, context, "change", allow_all=True)
    if prompt_shown:
        return

    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if not msg:
        return

    chat_id_str = str(update.effective_chat.id) if update.effective_chat else ""

    if is_all:
        servers = get_servers()
        await msg.reply_text(
            f"已收到全部 {len(servers)} 台服务器换IP请求，开始并行执行...\n"
            "网络可能会短暂中断；如果结果当时发不出去，机器人恢复后会自动补发。"
        )
        tasks = [do_change_single_server(s, chat_id=chat_id_str) for s in servers]
        results = await asyncio.gather(*tasks)
        lines = ["🌐【全部服务器换IP执行结果】"]
        for res in results:
            status_icon = "✅" if res.success else "❌"
            lines.append(f"\n{status_icon} 【{res.server_name}】: {res.status}\n{res.message}")
            if res.success and res.new_ip:
                lines.append(f"  • 新IP: {res.new_ip}")
            if res.dns_message:
                lines.append(f"  • DNS: {res.dns_message}")
        try:
            for s in servers:
                mark_sending_notify(True, server_id=s["id"])
            await msg.reply_text("\n".join(lines))
            for s in servers:
                mark_notification_sent(server_id=s["id"])
        except Exception as e:
            logger.warning(f"发送全部换IP结果失败: {e}")
            for s in servers:
                mark_sending_notify(False, server_id=s["id"])
    else:
        sid = server_cfg.get("id", "default")
        sname = server_cfg.get("name", sid)
        await msg.reply_text(
            f"已收到服务器 [{sname}] 换IP请求，马上开始执行。\n"
            "网络可能会短暂中断；如果结果当时发不出去，机器人恢复后会自动补发。"
        )
        res = await do_change_single_server(server_cfg, chat_id=chat_id_str)
        try:
            mark_sending_notify(True, server_id=sid)
            text = build_result_message(res)
            hint = ""
            if is_multi_server_mode():
                hint = f"\n\n💡 提示: 当前操作服务器为 [{sname}]，输入 /servers 查看列表，/use 切换"
            await msg.reply_text(text + hint)
            mark_notification_sent(server_id=sid)
        except Exception as e:
            logger.warning(f"发送换IP结果失败，将等待恢复后补发: {e}")
            mark_sending_notify(False, server_id=sid)
