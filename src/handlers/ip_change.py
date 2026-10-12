import asyncio
from typing import Any, Dict, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
from utils.task_locks import get_task_lock


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
    query = getattr(update, "callback_query", None)

    change_markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 重新换IP", callback_data="main_menu:change"),
            InlineKeyboardButton("🔙 返回主菜单", callback_data="main_menu:refresh"),
        ]
    ])

    if is_all:
        servers = get_servers()
        ack_text = (
            f"已收到全部 {len(servers)} 台服务器换IP请求，开始并行执行...\n"
            "网络可能会短暂中断；如果结果当时发不出去，机器人恢复后会自动补发。"
        )
        if query and query.message:
            try:
                await query.edit_message_text(ack_text)
                status_msg = query.message
            except Exception:
                status_msg = await msg.reply_text(ack_text)
        else:
            status_msg = await msg.reply_text(ack_text)

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
            final_text = "\n".join(lines)
            try:
                await status_msg.edit_text(final_text, reply_markup=change_markup)
            except Exception:
                await msg.reply_text(final_text, reply_markup=change_markup)
            for s in servers:
                mark_notification_sent(server_id=s["id"])
        except Exception as e:
            logger.warning(f"发送全部换IP结果失败: {e}")
            for s in servers:
                mark_sending_notify(False, server_id=s["id"])
    else:
        sid = server_cfg.get("id", "default")
        sname = server_cfg.get("name", sid)

        lock = get_task_lock("change", sid)
        if lock.locked():
            prefix = f"【{sname}】" if is_multi_server_mode() else ""
            notice = f"⚠️ {prefix}当前已有正在运行的【更换 IP】任务，请勿重复发起，请稍候。"
            if query and query.message:
                try:
                    await query.edit_message_text(notice, reply_markup=change_markup)
                    return
                except Exception:
                    pass
            await msg.reply_text(notice, reply_markup=change_markup)
            return

        if is_multi_server_mode():
            ack_text = (
                f"已收到服务器 [{sname}] 换IP请求，马上开始执行。\n"
                "网络可能会短暂中断；如果结果当时发不出去，机器人恢复后会自动补发。"
            )
        else:
            ack_text = (
                "已收到换IP请求，马上开始执行。\n"
                "网络可能会短暂中断；如果结果当时发不出去，机器人恢复后会自动补发。"
            )

        if query and query.message:
            try:
                await query.edit_message_text(ack_text)
                status_msg = query.message
            except Exception:
                status_msg = await msg.reply_text(ack_text)
        else:
            status_msg = await msg.reply_text(ack_text)

        res = await do_change_single_server(server_cfg, chat_id=chat_id_str)
        try:
            mark_sending_notify(True, server_id=sid)
            text = build_result_message(res)
            hint = ""
            if is_multi_server_mode():
                hint = f"\n\n💡 提示: 当前操作服务器为 [{sname}]，输入 /servers 查看列表，/use 切换"
            final_text = text + hint
            try:
                await status_msg.edit_text(final_text, reply_markup=change_markup)
            except Exception:
                await msg.reply_text(final_text, reply_markup=change_markup)
            mark_notification_sent(server_id=sid)
        except Exception as e:
            logger.warning(f"发送换IP结果失败，将等待恢复后补发: {e}")
            mark_sending_notify(False, server_id=sid)
