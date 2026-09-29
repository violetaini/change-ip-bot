import asyncio

from telegram import Update
from telegram.ext import ContextTypes

from config import config
from handlers.user_check import check_user_permission
from utils.logger import logger
from utils.network import call_boil_get_ip, check_ip_blocked


async def check_ip_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_permission(update):
        return

    user_id = update.effective_user.id
    user_name = update.effective_user.username
    full_name = update.effective_user.full_name
    logger.info(f"收到 check 命令，用户ID: {user_id}，用户名: {user_name}，全名: {full_name}")

    await update.message.reply_text(text="正在检查IP状态...")

    provider = str(config.get("ip_change_provider", "classic")).strip().lower()
    if provider == "boil":
        token = str(config.get("boil_api_token", "")).strip()
        base_url = str(config.get("boil_api_base_url", "https://ippanel.boil.network")).strip()
        if token:
            try:
                boil_ip = await asyncio.to_thread(call_boil_get_ip, base_url, token, 15)
                extra_lines = []
                from utils.remote_ssh import is_remote_ssh_enabled, run_remote_ssh_command, get_ssh_config
                if is_remote_ssh_enabled():
                    cfg = get_ssh_config()
                    try:
                        code, _ = await asyncio.to_thread(
                            run_remote_ssh_command,
                            "curl -s --connect-timeout 5 -o /dev/null -w '%{http_code}' https://www.itdog.cn",
                            timeout=10,
                        )
                        status_str = "正常" if code == 0 else "丢包/超时"
                        extra_lines.append(f"- 家宽至国内连通性(itdog): {status_str}")
                    except Exception as ex:
                        extra_lines.append(f"- 家宽至国内连通性(itdog): 探测超时")

                extra_text = ("\n" + "\n".join(extra_lines)) if extra_lines else ""
                await update.message.reply_text(
                    text=(
                        f"【Boil 模式当前住宅IP】\n"
                        f"- IP地址: {boil_ip}\n"
                        f"- 状态: 正常（通过 Boil 官方 API 获取）{extra_text}\n"
                        f"- 提示: 如需更换，可使用 /change 命令"
                    )
                )
                return
            except Exception as e:
                await update.message.reply_text(text=f"通过 Boil API 获取IP失败: {e}")
                return

    try:
        is_blocked, current_ip = await asyncio.to_thread(check_ip_blocked)
        if is_blocked:
            await update.message.reply_text(
                text=f"当前IP: {current_ip}\n出站连通性检测（至国内节点 www.itdog.cn）: 丢包率过高/连接超时\n如需更换，可使用 /change 命令"
            )
        else:
            await update.message.reply_text(
                text=f"当前IP: {current_ip}\n出站连通性检测（至国内节点 www.itdog.cn）: 正常"
            )
    except Exception as e:
        await update.message.reply_text(text=f"检查IP状态时出错: {str(e)}")
