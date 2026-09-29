from typing import Any, Dict, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import config, get_server_config, get_servers, is_multi_server_mode
from utils.state import get_user_selected_server, set_user_selected_server


def get_action_keyboard(command: str, allow_all: bool = True) -> InlineKeyboardMarkup:
    servers = get_servers()
    keyboard = []
    row = []
    for s in servers:
        s_id = s.get("id")
        s_name = s.get("name") or s_id
        btn = InlineKeyboardButton(s_name, callback_data=f"srv_act:{command}:{s_id}")
        row.append(btn)
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    bottom_row = []
    if allow_all and len(servers) > 1:
        bottom_row.append(InlineKeyboardButton("🌐 全部服务器", callback_data=f"srv_act:{command}:all"))
    bottom_row.append(InlineKeyboardButton("❌ 取消", callback_data="srv_act:cancel:none"))
    keyboard.append(bottom_row)
    return InlineKeyboardMarkup(keyboard)


async def resolve_target_server(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    command: str,
    allow_all: bool = True,
) -> Tuple[Optional[Dict[str, Any]], bool, bool]:
    """
    解析当前命令的目标服务器。
    返回 (server_config, is_all, prompt_shown):
      - 如果 prompt_shown 为 True，说明已向用户发送了选择键盘，handler 应直接 return。
      - 如果 is_all 为 True，说明用户请求对所有服务器批量执行。
      - 如果 server_config 不为 None，说明确定了单个目标服务器。
    """
    servers = get_servers()
    if len(servers) <= 1:
        return servers[0], False, False

    args = list(context.args) if context.args else []
    user_id = update.effective_user.id if update.effective_user else 0

    # 1. 检查命令行首个参数是否匹配服务器 ID 或 "all"
    if args:
        first_arg = args[0].strip().lower()
        if allow_all and first_arg in ("all", "全部"):
            context.args.pop(0)
            return None, True, False
        matched = get_server_config(first_arg)
        if matched:
            context.args.pop(0)
            return matched, False, False

    # 2. 检查用户此前是否通过 /use 设定了默认操作服务器
    selected_id = get_user_selected_server(user_id)
    if selected_id:
        matched = get_server_config(selected_id)
        if matched:
            return matched, False, False

    # 3. 未指定且未设定默认，弹出 Inline 键盘供用户点选
    reply_markup = get_action_keyboard(command, allow_all=allow_all)
    cmd_desc = {
        "check": "检查 IP 状态与连通性",
        "change": "更换 IP 并同步 DNS",
        "speedtest": "网络测速",
        "stream": "检测流媒体解锁",
        "quality": "检测 IP 质量报告",
        "ip_status": "查看换 IP 配置与冷却",
        "ping": "测试网络延迟 Ping",
        "use": "选择默认操作服务器",
    }.get(command, command)

    prompt_text = f"当前配置了多台服务器，请选择要执行【{cmd_desc}】的目标："
    if update.message:
        await update.message.reply_text(prompt_text, reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.message.reply_text(prompt_text, reply_markup=reply_markup)

    return None, False, True
