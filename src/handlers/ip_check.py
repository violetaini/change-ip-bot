import asyncio

from telegram import Update
from telegram.ext import ContextTypes

from config import config
from handlers.user_check import check_user_permission
from utils.logger import logger
from utils.network import (
    call_boil_get_ip,
    check_ip_blocked,
    get_current_ip,
    probe_domestic_http,
    resolve_mainland_target,
)


async def check_ip_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_permission(update):
        return

    user_id = update.effective_user.id
    user_name = update.effective_user.username
    full_name = update.effective_user.full_name
    logger.info(f"收到 check 命令，用户ID: {user_id}，用户名: {user_name}，全名: {full_name}")

    await update.message.reply_text(text="正在检查IP与境内连通性...")

    provider = str(config.get("ip_change_provider", "classic")).strip().lower()
    from utils.remote_ssh import is_remote_ssh_enabled, run_remote_ssh_command, set_cached_host_ip

    if provider == "boil":
        token = str(config.get("boil_api_token", "")).strip()
        base_url = str(config.get("boil_api_base_url", "https://ippanel.boil.network")).strip()
        if token:
            try:
                boil_ip = await asyncio.to_thread(call_boil_get_ip, base_url, token, 15)
                # 实时同步最新住宅IP到本地DNS映射，确保后续所有SSH诊断命令立即可用
                raw_host = str(config.get("remote_ssh_host") or "").strip()
                if raw_host and boil_ip:
                    set_cached_host_ip(raw_host, boil_ip)

                lines = [
                    "【Boil 模式当前住宅IP】",
                    f"- IPv4 地址: {boil_ip}",
                ]

                ssh_enabled = is_remote_ssh_enabled()
                ssh_fn = run_remote_ssh_command if ssh_enabled else None

                # 动态解析国内电信直连节点
                v4_target, v6_target = await asyncio.to_thread(resolve_mainland_target)

                # 探测 IPv4 境内连通性 (带3次重试与超时)
                ok4, desc4, _ = await asyncio.to_thread(
                    probe_domestic_http,
                    v4_target,
                    4,
                    domain="v.qq.com",
                    retries=3,
                    timeout=4,
                    run_ssh_fn=ssh_fn,
                )
                lines.append(f"  • 境内连通性 (v.qq.com 电信): {desc4}")

                # 检测公网 IPv6
                if ssh_enabled:
                    code_v6, out_v6 = await asyncio.to_thread(
                        run_remote_ssh_command,
                        "curl -6 -s --connect-timeout 3 -m 5 https://api64.ipify.org",
                        timeout=8,
                    )
                    v6_ip = out_v6.strip() if (code_v6 == 0 and ":" in out_v6) else ""
                else:
                    def _get_local_v6():
                        try:
                            import subprocess
                            res = subprocess.run(
                                ["curl", "-6", "-s", "--connect-timeout", "3", "-m", "5", "https://api64.ipify.org"],
                                capture_output=True,
                                text=True,
                                timeout=8,
                            )
                            return res.stdout.strip() if (res.returncode == 0 and ":" in res.stdout) else ""
                        except Exception:
                            return ""
                    v6_ip = await asyncio.to_thread(_get_local_v6)

                if v6_ip:
                    ok6, desc6, _ = await asyncio.to_thread(
                        probe_domestic_http,
                        v6_target,
                        6,
                        domain="v.qq.com",
                        retries=3,
                        timeout=4,
                        run_ssh_fn=ssh_fn,
                    )
                    lines.append(f"- IPv6 地址: {v6_ip}")
                    lines.append(f"  • 境内连通性 (v.qq.com 电信): {desc6}")
                else:
                    lines.append("- IPv6 地址: 未分配 / 不支持")

                lines.append("- 状态: 正常（通过 Boil 官方 API 获取）")
                lines.append("- 提示: 如需更换，可使用 /change 命令")
                await update.message.reply_text(text="\n".join(lines))
                return
            except Exception as e:
                await update.message.reply_text(text=f"通过 Boil API 获取IP失败: {e}")
                return

    # Classic 模式
    try:
        ssh_enabled = is_remote_ssh_enabled()
        ssh_fn = run_remote_ssh_command if ssh_enabled else None

        if ssh_enabled:
            code_v4, out_v4 = await asyncio.to_thread(
                run_remote_ssh_command,
                "curl -4 -s --connect-timeout 3 -m 5 https://api.ipify.org",
                timeout=8,
            )
            current_v4 = out_v4.strip() if code_v4 == 0 else "未知"
        else:
            current_v4 = await asyncio.to_thread(get_current_ip)

        v4_target, v6_target = await asyncio.to_thread(resolve_mainland_target)

        ok4, desc4, _ = await asyncio.to_thread(
            probe_domestic_http,
            v4_target,
            4,
            domain="v.qq.com",
            retries=3,
            timeout=4,
            run_ssh_fn=ssh_fn,
        )

        if ssh_enabled:
            code_v6, out_v6 = await asyncio.to_thread(
                run_remote_ssh_command,
                "curl -6 -s --connect-timeout 3 -m 5 https://api64.ipify.org",
                timeout=8,
            )
            v6_ip = out_v6.strip() if (code_v6 == 0 and ":" in out_v6) else ""
        else:
            def _get_local_v6_classic():
                try:
                    import subprocess
                    res = subprocess.run(
                        ["curl", "-6", "-s", "--connect-timeout", "3", "-m", "5", "https://api64.ipify.org"],
                        capture_output=True,
                        text=True,
                        timeout=8,
                    )
                    return res.stdout.strip() if (res.returncode == 0 and ":" in res.stdout) else ""
                except Exception:
                    return ""
            v6_ip = await asyncio.to_thread(_get_local_v6_classic)

        lines = [
            "【当前IP状态】",
            f"- IPv4 地址: {current_v4}",
            f"  • 境内连通性 (v.qq.com 电信): {desc4}",
        ]

        if v6_ip:
            ok6, desc6, _ = await asyncio.to_thread(
                probe_domestic_http,
                v6_target,
                6,
                domain="v.qq.com",
                retries=3,
                timeout=4,
                run_ssh_fn=ssh_fn,
            )
            lines.append(f"- IPv6 地址: {v6_ip}")
            lines.append(f"  • 境内连通性 (v.qq.com 电信): {desc6}")
        else:
            lines.append("- IPv6 地址: 未分配 / 不支持")

        lines.append("- 提示: 如需更换，可使用 /change 命令")
        await update.message.reply_text(text="\n".join(lines))
    except Exception as e:
        await update.message.reply_text(text=f"检查IP状态时出错: {str(e)}")
