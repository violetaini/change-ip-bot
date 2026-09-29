import asyncio
from typing import Any, Dict, Optional

from telegram import Update
from telegram.ext import ContextTypes

from config import config, get_servers, is_multi_server_mode
from handlers.server_selection import resolve_target_server
from handlers.user_check import check_user_permission
from utils.logger import logger
from utils.network import (
    call_boil_get_ip,
    check_ip_blocked,
    get_current_ip,
    probe_domestic_http,
    resolve_mainland_target,
)


async def do_check_single_server(server_cfg: Optional[Dict[str, Any]] = None) -> str:
    cfg = server_cfg if server_cfg is not None else config
    sid = str(cfg.get("id") or "default").strip()
    sname = str(cfg.get("name") or sid).strip()
    provider = str(cfg.get("ip_change_provider") or "generic").strip().lower()
    from utils.remote_ssh import is_remote_ssh_enabled, run_remote_ssh_command, set_cached_host_ip

    ssh_enabled = is_remote_ssh_enabled(cfg)
    ssh_fn = (
        (lambda cmd, timeout=300, input_data=None: run_remote_ssh_command(
            cmd, timeout=timeout, input_data=input_data, server_config=cfg
        ))
        if ssh_enabled
        else None
    )

    if provider == "boil":
        token = str(cfg.get("boil_api_token", "")).strip()
        base_url = str(cfg.get("boil_api_base_url", "https://ippanel.boil.network")).strip()
        if token:
            try:
                boil_ip = await asyncio.to_thread(call_boil_get_ip, base_url, token, 15)
                raw_host = str(cfg.get("remote_ssh_host") or "").strip()
                if raw_host and boil_ip:
                    set_cached_host_ip(raw_host, boil_ip)

                title = f"【{sname} (Boil住宅)】" if is_multi_server_mode() else "【Boil 模式当前住宅IP】"
                lines = [
                    title,
                    f"- IPv4 地址: {boil_ip}",
                ]
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
                lines.append(f"  • 境内连通性 (v.qq.com 电信): {desc4}")

                if ssh_enabled:
                    code_v6, out_v6 = await asyncio.to_thread(
                        run_remote_ssh_command,
                        "curl -6 -s --connect-timeout 3 -m 5 https://api64.ipify.org",
                        timeout=8,
                        server_config=cfg,
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
                return "\n".join(lines)
            except Exception as e:
                prefix = f"【{sname}】" if is_multi_server_mode() else ""
                return f"{prefix}通过 Boil API 获取IP失败: {e}"

    # Generic & Fachost 模式
    try:
        from services.ip_change_service import get_active_public_ipv4
        current_v4 = await asyncio.to_thread(get_active_public_ipv4, 8, cfg)
        if not current_v4:
            current_v4 = "未知"

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
                server_config=cfg,
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

        mode_desc = "Fachost 专用模式" if provider in ("fachost", "classic") else "Generic 通用模式"
        title = f"【{sname} ({mode_desc})】" if is_multi_server_mode() else "【当前IP状态】"
        lines = [
            title,
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

        return "\n".join(lines)
    except Exception as e:
        prefix = f"【{sname}】" if is_multi_server_mode() else ""
        return f"{prefix}检查IP状态时出错: {e}"


async def check_ip_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_permission(update):
        return

    user_id = update.effective_user.id if update.effective_user else 0
    logger.info(f"收到 check 命令，用户ID: {user_id}")

    server_cfg, is_all, prompt_shown = await resolve_target_server(update, context, "check", allow_all=True)
    if prompt_shown:
        return

    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if not msg:
        return

    status_msg = await msg.reply_text(text="正在检查IP与境内连通性...")

    if is_all:
        servers = get_servers()
        tasks = [do_check_single_server(s) for s in servers]
        results = await asyncio.gather(*tasks)
        final_text = "🌐【所有服务器 IP 状态总览】\n\n" + "\n\n".join(results)
        await status_msg.edit_text(text=final_text)
    else:
        result = await do_check_single_server(server_cfg)
        hint = ""
        if is_multi_server_mode():
            hint = f"\n\n💡 提示: 当前操作服务器为 [{server_cfg.get('name', server_cfg.get('id'))}]，输入 /servers 查看列表，/use 切换"
        await status_msg.edit_text(text=result + hint)
