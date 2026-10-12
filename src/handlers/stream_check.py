import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from typing import Any, Dict, Optional, Tuple

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import config, is_multi_server_mode, DEFAULT_STREAM_CMD
from handlers.server_selection import resolve_target_server
from handlers.user_check import check_user_permission
from utils.logger import logger
from utils.redact import redact_text
from utils.task_locks import get_task_lock


DEFAULT_STREAM_API = "https://api.ip.sb/geoip?callback=getgeoip"
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
SEPARATOR_RE = re.compile(r"^[=\-_*#\s|+]+$")

SERVICE_PATTERNS = [
    ("YouTube CDN", re.compile(r"\byoutube\b.*\bcdn\b|\bcdn\b.*\byoutube\b|油管.*cdn", re.IGNORECASE)),
    ("Netflix CDN", re.compile(r"\bnetflix\b.*\bcdn\b|\bcdn\b.*\bnetflix\b|奈飞.*cdn|cdn.*奈飞", re.IGNORECASE)),
    ("Netflix", re.compile(r"\bnetflix\b|奈飞", re.IGNORECASE)),
    ("Disney+", re.compile(r"\bdisney\+?\b|迪士尼", re.IGNORECASE)),
    ("YouTube Premium", re.compile(r"\byoutube\s+premium\b|油管会员", re.IGNORECASE)),
    ("TikTok", re.compile(r"\btiktok\b", re.IGNORECASE)),
    ("ChatGPT/OpenAI", re.compile(r"\bchatgpt\b|\bopenai\b", re.IGNORECASE)),
    ("Prime Video", re.compile(r"\bprime\s+video\b|\bamazon\b", re.IGNORECASE)),
    ("Spotify", re.compile(r"\bspotify\b", re.IGNORECASE)),
    ("HBO/Max", re.compile(r"\bhbo\b|\bmax\b", re.IGNORECASE)),
    ("Hulu", re.compile(r"\bhulu\b", re.IGNORECASE)),
    ("DAZN", re.compile(r"\bdazn\b", re.IGNORECASE)),
    ("BBC iPlayer", re.compile(r"\bbbc\s+iplayer\b", re.IGNORECASE)),
    ("Abema", re.compile(r"\babema\b", re.IGNORECASE)),
    ("Bahamut", re.compile(r"\bbahamut\b|巴哈姆特", re.IGNORECASE)),
    ("TVB", re.compile(r"\btvb\b|mytvsuper", re.IGNORECASE)),
    ("Viu", re.compile(r"\bviu\b", re.IGNORECASE)),
    ("KKTV", re.compile(r"\bkktv\b", re.IGNORECASE)),
    ("LiTV", re.compile(r"\blitv\b", re.IGNORECASE)),
    ("Niconico", re.compile(r"\bniconico\b|ニコニコ", re.IGNORECASE)),
]


def strip_ansi(text: str) -> str:
    text = ANSI_RE.sub("", str(text or ""))
    text = text.replace("\r", "\n")
    return CONTROL_RE.sub("", text)


def run_stream_command(cmd: str, auto_input: str, timeout: int, server_config: Optional[Dict[str, Any]] = None) -> tuple[int, str, float]:
    from utils.remote_ssh import is_remote_ssh_enabled, run_remote_ssh_command
    if is_remote_ssh_enabled(server_config):
        logger.info("通过远程家宽 SSH 执行流媒体解锁检测脚本")
        started = time.monotonic()
        remote_cmd = f"cd /tmp && {cmd}"
        code, out = run_remote_ssh_command(remote_cmd, timeout=max(30, int(timeout)), input_data=f"{auto_input.rstrip()}\n", server_config=server_config)
        elapsed = time.monotonic() - started
        return code, strip_ansi(out).strip(), elapsed

    clean_env = {
        "PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
        "HOME": "/tmp",
        "TERM": "xterm-256color",
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "en_US.UTF-8"),
    }

    run_kwargs = {
        "shell": True,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": max(30, int(timeout)),
        "input": f"{auto_input.rstrip()}\n",
        "env": clean_env,
    }
    if os.name != "nt" and os.path.exists("/tmp"):
        run_kwargs["cwd"] = "/tmp"

    if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
        try:
            import pwd
            pwd.getpwnam("nobody")
            run_kwargs["user"] = "nobody"
            run_kwargs["group"] = "nogroup"
        except (KeyError, ImportError, Exception) as e:
            logger.debug(f"未找到 nobody/nogroup 用户或不支持降权: {e}")

    bash_path = shutil.which("bash")
    if bash_path:
        run_kwargs["executable"] = bash_path
    elif "<(" in cmd:
        raise RuntimeError("当前流媒体检测命令需要 bash，但系统中未找到 bash")

    started = time.monotonic()
    process = subprocess.run(cmd, **run_kwargs)
    elapsed = time.monotonic() - started
    output = (process.stdout or "") + "\n" + (process.stderr or "")
    return process.returncode, strip_ansi(output).strip(), elapsed


def is_noise_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if SEPARATOR_RE.match(stripped):
        return True
    lowered = stripped.lower()
    noise_keywords = (
        "github.com",
        "regionrestrictioncheck",
        "选择",
        "请选择",
        "输入",
        "press",
        "checking",
        "检测中",
        "正在测试",
        "测试时间",
        "免责声明",
    )
    return any(keyword in lowered for keyword in noise_keywords)


def normalize_result(text: str) -> str:
    text = re.sub(r"^[\s:：\-=>\[\]*·•|]+", "", text.strip())
    text = re.sub(r"\s+", " ", text)
    return text[:180].strip()


def split_service_line(line: str, display_name: str) -> str:
    for separator in (":", "："):
        if separator in line:
            before, after = line.split(separator, 1)
            if display_name.lower().split("/")[0] in before.lower() or len(before) <= 80:
                return normalize_result(after)
    return ""


def find_service_results(lines: list[str]) -> list[tuple[str, str]]:
    results = []
    seen = set()

    for idx, line in enumerate(lines):
        if is_noise_line(line):
            continue

        for display_name, pattern in SERVICE_PATTERNS:
            if display_name in seen or not pattern.search(line):
                continue

            result = split_service_line(line, display_name)
            if not result:
                for next_line in lines[idx + 1:idx + 5]:
                    if is_noise_line(next_line):
                        continue
                    if any(other_pattern.search(next_line) for _, other_pattern in SERVICE_PATTERNS):
                        break
                    result = normalize_result(next_line)
                    if result:
                        break

            if result:
                results.append((display_name, result))
                seen.add(display_name)
            break

    return results


def extract_relevant_lines(lines: list[str]) -> list[str]:
    relevant = []
    keyword_re = re.compile(
        r"netflix|disney|youtube|tiktok|chatgpt|openai|prime|spotify|hbo|max|hulu|dazn|bbc|abema|bahamut|tvb|viu|kktv|litv|niconico|解锁|区域|region|yes|no",
        re.IGNORECASE,
    )
    for line in lines:
        stripped = normalize_result(line)
        if stripped and not is_noise_line(stripped) and keyword_re.search(stripped):
            relevant.append(stripped)
    return relevant[-25:]


def split_sections_by_ip_version(lines: list[str]) -> dict[str, dict]:
    """Split script output lines into IPv4 and IPv6 sections with network provider info."""
    sections = {
        "v4": {"network": "", "lines": []},
        "v6": {"network": "", "lines": []},
        "general": {"network": "", "lines": []},
    }
    current_key = "general"

    for line in lines:
        stripped = strip_ansi(line).strip()
        if re.search(r"正在测试\s*IPv4|Checking Results Under\s*IPv4", stripped, re.IGNORECASE):
            current_key = "v4"
            continue
        elif re.search(r"正在测试\s*IPv6|Checking Results Under\s*IPv6", stripped, re.IGNORECASE):
            current_key = "v6"
            continue

        net_match = re.search(r"(?:您的网络为|Your Network Provider)\s*:\s*(.+)", stripped, re.IGNORECASE)
        if net_match and current_key in ("v4", "v6") and not sections[current_key]["network"]:
            sections[current_key]["network"] = net_match.group(1).strip()
            continue

        sections[current_key]["lines"].append(line)

    return sections


def build_stream_summary(return_code: int, output: str, elapsed: float) -> str:
    redacted = redact_text(strip_ansi(output))
    lines = [line.strip() for line in redacted.splitlines() if line.strip()]
    sections = split_sections_by_ip_version(lines)

    v4_results = find_service_results(sections["v4"]["lines"]) if sections["v4"]["lines"] else []
    v6_results = find_service_results(sections["v6"]["lines"]) if sections["v6"]["lines"] else []

    is_dual_stack = bool(v4_results and v6_results)
    stack_tag = "双栈" if is_dual_stack else ("单栈 IPv4" if v4_results else ("单栈 IPv6" if v6_results else ""))

    header = [
        f"流媒体检测简报 ({stack_tag})" if stack_tag else "流媒体检测简报",
        f"脚本返回码: {return_code}",
        f"耗时: {elapsed:.0f} 秒",
    ]

    body = []
    if v4_results:
        v4_info = f" ({sections['v4']['network']})" if sections['v4']['network'] else ""
        body.append(f"\n🌐【IPv4 解锁结果】{v4_info}")
        body.extend(f"- {name}: {result}" for name, result in v4_results[:18])

    if v6_results:
        v6_info = f" ({sections['v6']['network']})" if sections['v6']['network'] else ""
        body.append(f"\n🌐【IPv6 解锁结果】{v6_info}")
        body.extend(f"- {name}: {result}" for name, result in v6_results[:18])

    if not v4_results and not v6_results:
        # Fallback to general parsing if headers were not matched
        general_results = find_service_results(lines)
        if general_results:
            body.append("\n重点结果:")
            body.extend(f"- {name}: {result}" for name, result in general_results[:18])
        else:
            relevant = extract_relevant_lines(lines)
            body.append("\n未能结构化提取重点服务，以下是有效输出摘录:")
            body.extend(f"- {line}" for line in relevant[:25])
            if not relevant:
                body.append("- 脚本没有返回可识别的检测结果")

    message = "\n".join(header + body)
    if len(message) > 3800:
        message = message[:3700].rstrip() + "\n\n输出较长，已截断。"
    return message


REGION_NAMES = {
    "0": "仅跨国平台",
    "1": "跨国 + 台湾平台",
    "2": "跨国 + 香港平台",
    "3": "跨国 + 日本平台",
    "4": "跨国 + 北美平台",
    "5": "跨国 + 南美平台",
    "6": "跨国 + 欧洲平台",
    "7": "跨国 + 大洋洲平台",
    "8": "跨国 + 韩国平台",
    "9": "跨国 + 东南亚平台",
    "10": "AI 平台",
    "11": "跨国 + 非洲平台",
    "99": "体育直播平台",
}

COUNTRY_TO_REGION_INPUT = {
    # 1: 跨国 + 台湾平台
    "TW": "1",
    # 2: 跨国 + 香港平台
    "HK": "2",
    "MO": "2",
    # 3: 跨国 + 日本平台
    "JP": "3",
    # 4: 跨国 + 北美平台
    "US": "4",
    "CA": "4",
    # 5: 跨国 + 南美平台
    "BR": "5",
    "AR": "5",
    "CL": "5",
    "CO": "5",
    "PE": "5",
    "MX": "5",
    "UY": "5",
    "VE": "5",
    "EC": "5",
    "BO": "5",
    "PY": "5",
    # 6: 跨国 + 欧洲平台
    "GB": "6",
    "UK": "6",
    "DE": "6",
    "FR": "6",
    "NL": "6",
    "IT": "6",
    "ES": "6",
    "RU": "6",
    "UA": "6",
    "PL": "6",
    "CH": "6",
    "SE": "6",
    "NO": "6",
    "FI": "6",
    "DK": "6",
    "IE": "6",
    "BE": "6",
    "AT": "6",
    "PT": "6",
    "CZ": "6",
    "GR": "6",
    "RO": "6",
    "HU": "6",
    "TR": "6",
    "IS": "6",
    "LU": "6",
    "EE": "6",
    "LV": "6",
    "LT": "6",
    "SK": "6",
    "BG": "6",
    "HR": "6",
    "SI": "6",
    "CY": "6",
    "MT": "6",
    # 7: 跨国 + 大洋洲平台
    "AU": "7",
    "NZ": "7",
    # 8: 跨国 + 韩国平台
    "KR": "8",
    # 9: 跨国 + 东南亚平台
    "SG": "9",
    "MY": "9",
    "TH": "9",
    "VN": "9",
    "ID": "9",
    "PH": "9",
    "KH": "9",
    "MM": "9",
    "LA": "9",
    "BN": "9",
    # 11: 跨国 + 非洲平台
    "ZA": "11",
    "EG": "11",
    "NG": "11",
    "KE": "11",
    "MA": "11",
    "GH": "11",
    "TN": "11",
    "DZ": "11",
    "UG": "11",
}


def parse_country_code_from_response(raw_text: str) -> Optional[str]:
    """Parse country_code from JSON or JSONP response (such as IP.SB geoip API)."""
    if not raw_text:
        return None
    match = re.search(r"\{.*\}", str(raw_text), re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            code = data.get("country_code")
            if code and isinstance(code, str):
                return code.strip().upper()
        except Exception:
            pass
    return None


_STREAM_GEO_CACHE: Dict[str, Tuple[str, float]] = {}


def get_cached_country_code(server_id: str = "default") -> Optional[str]:
    """Get cached country code from memory or persistent server state."""
    sid = str(server_id or "default").strip()
    entry = _STREAM_GEO_CACHE.get(sid)
    if entry and entry[0]:
        return entry[0]

    try:
        from utils.state import load_server_state
        s_state = load_server_state(sid)
        cached = s_state.get("cached_stream_country")
        if cached and isinstance(cached, str):
            code = cached.strip().upper()
            ts = float(s_state.get("cached_stream_country_at", 0) or 0)
            _STREAM_GEO_CACHE[sid] = (code, ts)
            return code
    except Exception as e:
        logger.debug(f"读取流媒体区域缓存异常 ({sid}): {e}")
    return None


def set_cached_country_code(server_id: str, country_code: str) -> None:
    """Save cached country code to memory and persistent server state."""
    sid = str(server_id or "default").strip()
    code = str(country_code or "").strip().upper()
    if not code:
        return
    now = time.time()
    _STREAM_GEO_CACHE[sid] = (code, now)
    try:
        from utils.state import update_server_state_keys
        update_server_state_keys(sid, {
            "cached_stream_country": code,
            "cached_stream_country_at": now,
        })
    except Exception as e:
        logger.debug(f"持久化流媒体区域缓存异常 ({sid}): {e}")


def detect_ip_country_code(server_config: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Detect exit IP country code via IP.SB API (remotely via SSH if configured, else locally)."""
    from utils.remote_ssh import is_remote_ssh_enabled, run_remote_ssh_command

    cfg = server_config if server_config is not None else config
    url = str(cfg.get("stream_check_api") or DEFAULT_STREAM_API).strip()
    try:
        raw_timeout = int(cfg.get("stream_check_api_timeout", 3))
    except (ValueError, TypeError):
        raw_timeout = 3
    api_timeout = max(1, min(raw_timeout, 30))

    try:
        if is_remote_ssh_enabled(server_config):
            cmd = f"curl -s -m {api_timeout} {shlex.quote(url)}"
            code, out = run_remote_ssh_command(cmd, timeout=api_timeout + 5, server_config=server_config)
            if code == 0 and out:
                return parse_country_code_from_response(out)
        else:
            resp = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                timeout=api_timeout,
            )
            if resp.ok:
                return parse_country_code_from_response(resp.text)
    except Exception as e:
        logger.warning(f"自动检测出口 IP 归属地失败 ({url}, timeout={api_timeout}s): {e}")
    return None


_PENDING_STREAM_SELECTIONS: Dict[str, Dict[str, Any]] = {}


async def stream_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    if not await check_user_permission(update):
        return
    await query.answer()

    parts = query.data.split(":")
    if len(parts) != 4 or parts[0] != "stream_sel":
        return

    _, sid, token, option = parts
    state = _PENDING_STREAM_SELECTIONS.get(token)
    if state:
        state["selected_input"] = option
        state["event"].set()
    else:
        try:
            await query.answer("⚠️ 该选择已超时并失效", show_alert=True)
        except Exception:
            pass


def validate_and_normalize_region(arg: str) -> Tuple[bool, Optional[str], Optional[str]]:
    clean = str(arg or "").strip()
    if not clean:
        return False, None, "参数不能为空"

    lowered = clean.lower()
    if lowered in ("auto", "api", "clear", "reset", "none"):
        return True, "auto", None

    if clean in REGION_NAMES:
        return True, clean, None

    upper_cc = clean.upper()
    if upper_cc in COUNTRY_TO_REGION_INPUT:
        return True, COUNTRY_TO_REGION_INPUT[upper_cc], None

    valid_keys = ", ".join([f"{k} ({v.split(' ')[-1]})" for k, v in REGION_NAMES.items()])
    return False, None, (
        f"❌ 无效的位置参数: `{clean}`\n\n"
        f"合法区域编号: {valid_keys}\n"
        f"常用国家代码: HK (香港), JP (日本), US (北美), TW (台湾), SG (东南亚) 等\n"
        f"输入 `auto` 可恢复 API 自动探测"
    )


def apply_stream_region_setting(normalized_input: str) -> str:
    from config import config
    try:
        from bot import persist_config_values
    except ImportError:
        persist_config_values = None

    if normalized_input == "auto":
        config["stream_check_auto_detect"] = True
        if persist_config_values:
            try:
                persist_config_values({"stream_check_auto_detect": True})
            except Exception as e:
                logger.warning(f"持久化 stream_check 配置失败: {e}")
        return (
            "✅ 已成功切换为【🤖 API 自动探测模式】！\n\n"
            "• 后续执行 /stream 将优先通过 IP.SB 实时探测当前出口 IP 归属地。\n"
            "• 若探测超时将优先使用历史缓存，首次无缓存时弹窗提供 1 分钟手动选择。"
        )
    else:
        region_desc = REGION_NAMES.get(normalized_input, f"区域 {normalized_input}")
        config["stream_check_auto_detect"] = False
        config["stream_check_input"] = normalized_input
        if persist_config_values:
            try:
                persist_config_values({
                    "stream_check_auto_detect": False,
                    "stream_check_input": normalized_input,
                })
            except Exception as e:
                logger.warning(f"持久化 stream_check 配置失败: {e}")
        return (
            f"✅ 已成功设定固定检测区域为:【{region_desc}】(编号 {normalized_input})！\n\n"
            "🔒【互斥机制生效】: 已自动关闭 API 定位获取。\n"
            "• 后续执行 /stream 将直接检测此区域，不再向外部 API 发起定位探测。\n"
            "• 如需恢复 API 自动识别，随时发送 `/set_stream_region auto`。"
        )


async def set_stream_region_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_permission(update):
        return

    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if not msg:
        return

    if context.args:
        raw_arg = context.args[0].strip()
        is_valid, norm_val, err_msg = validate_and_normalize_region(raw_arg)
        if not is_valid:
            await msg.reply_text(err_msg, parse_mode="Markdown")
            return

        reply_text = apply_stream_region_setting(norm_val)
        await msg.reply_text(reply_text)
        return

    auto_detect = bool(config.get("stream_check_auto_detect", True))
    current_input = str(config.get("stream_check_input", "2")).strip()
    current_name = REGION_NAMES.get(current_input, f"编号 {current_input}")

    if auto_detect:
        status_text = "🤖【API 自动探测模式】 (每次自动获取出口归属并匹配区域)"
    else:
        status_text = f"📌【管理员固定位置模式】: {current_name} (编号 {current_input}，已禁用 API 探测)"

    text = (
        f"📍【流媒体检测区域设置】\n\n"
        f"• 当前工作状态: {status_text}\n\n"
        f"💡 您可直接发送命令设置：\n"
        f"  • `/set_stream_region 3` (锁定日本)\n"
        f"  • `/set_stream_region US` (锁定北美)\n"
        f"  • `/set_stream_region auto` (恢复 API 自动探测)\n\n"
        f"或点击下方按钮快速切换："
    )

    keyboard = [
        [
            InlineKeyboardButton("🇹🇼 台湾 (1)", callback_data="set_stream_reg:1"),
            InlineKeyboardButton("🇭🇰 香港 (2)", callback_data="set_stream_reg:2"),
            InlineKeyboardButton("🇯🇵 日本 (3)", callback_data="set_stream_reg:3"),
        ],
        [
            InlineKeyboardButton("🇺🇸 北美 (4)", callback_data="set_stream_reg:4"),
            InlineKeyboardButton("🇧🇷 南美 (5)", callback_data="set_stream_reg:5"),
            InlineKeyboardButton("🇪🇺 欧洲 (6)", callback_data="set_stream_reg:6"),
        ],
        [
            InlineKeyboardButton("🇦🇺 大洋洲 (7)", callback_data="set_stream_reg:7"),
            InlineKeyboardButton("🇰🇷 韩国 (8)", callback_data="set_stream_reg:8"),
            InlineKeyboardButton("🇸🇬 东南亚 (9)", callback_data="set_stream_reg:9"),
        ],
        [
            InlineKeyboardButton("🌐 仅跨国 (0)", callback_data="set_stream_reg:0"),
            InlineKeyboardButton("🤖 AI平台 (10)", callback_data="set_stream_reg:10"),
            InlineKeyboardButton("⚽ 体育直播 (99)", callback_data="set_stream_reg:99"),
        ],
        [
            InlineKeyboardButton("🔄 恢复为【API 自动探测】模式", callback_data="set_stream_reg:auto"),
        ],
        [
            InlineKeyboardButton("🔙 返回主菜单", callback_data="main_menu:refresh"),
        ],
    ]

    query = getattr(update, "callback_query", None)
    if query and query.message:
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
            return
        except Exception as e:
            if "Message is not modified" in str(e):
                return
            logger.warning(f"edit_message_text 失败: {e}")
    await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def set_stream_region_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    if not await check_user_permission(update):
        await query.answer("⛔ 无权限操作", show_alert=True)
        return

    await query.answer()
    val = query.data.split(":", 1)[1] if ":" in query.data else ""
    is_valid, norm_val, err_msg = validate_and_normalize_region(val)
    if not is_valid:
        await query.answer(err_msg or "参数无效", show_alert=True)
        return

    reply_text = apply_stream_region_setting(norm_val)
    res_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📍 重新设置区域", callback_data="main_menu:stream_region"),
            InlineKeyboardButton("🔙 返回主菜单", callback_data="main_menu:refresh"),
        ]
    ])
    try:
        await query.edit_message_text(reply_text, reply_markup=res_keyboard)
    except Exception:
        await query.message.reply_text(reply_text, reply_markup=res_keyboard)


async def stream_check_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_permission(update):
        return

    if not config.get("stream_check_enabled", True):
        disabled_text = "流媒体检测未启用。"
        query = getattr(update, "callback_query", None)
        menu_k = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回主菜单", callback_data="main_menu:refresh")]])
        if query and query.message:
            try:
                await query.edit_message_text(disabled_text, reply_markup=menu_k)
                return
            except Exception:
                pass
        msg = update.message or (update.callback_query.message if update.callback_query else None)
        if msg:
            await msg.reply_text(disabled_text, reply_markup=menu_k)
        return

    user_id = update.effective_user.id if update.effective_user else 0
    logger.info(f"收到 stream 命令，用户ID: {user_id}")

    server_cfg, is_all, prompt_shown = await resolve_target_server(update, context, "stream", allow_all=False)
    if prompt_shown:
        return

    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if not msg:
        return

    cfg = server_cfg if server_cfg is not None else config
    sid = cfg.get("id", "default")
    sname = cfg.get("name", sid)

    lock = get_task_lock("stream", sid)
    if lock.locked():
        prefix = f"【{sname}】" if is_multi_server_mode() else ""
        notice = f"⚠️ {prefix}当前已有正在运行的【流媒体解锁检测】任务，请勿重复发起，请等待完成后再试。"
        stream_back_markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🎬 重新检测流媒体", callback_data="main_menu:stream"),
                InlineKeyboardButton("🔙 返回主菜单", callback_data="main_menu:refresh"),
            ]
        ])
        query = getattr(update, "callback_query", None)
        if query and query.message:
            try:
                await query.edit_message_text(notice, reply_markup=stream_back_markup)
            except Exception:
                await msg.reply_text(notice, reply_markup=stream_back_markup)
        else:
            await msg.reply_text(notice, reply_markup=stream_back_markup)
        return

    async with lock:
        user_args = context.args or []
        detected_country = None
        if user_args:
            first_arg = str(user_args[0]).strip()
            if first_arg in REGION_NAMES or (first_arg.isdigit() and len(first_arg) <= 2):
                auto_input = first_arg
            else:
                auto_input = str(cfg.get("stream_check_input", "2")).strip()
            region_desc = REGION_NAMES.get(auto_input, f"模式 {auto_input}")
        else:
            auto_detect = bool(cfg.get("stream_check_auto_detect", True))
            if not auto_detect:
                # 管理员固定设定模式（互斥：不请求 API）
                auto_input = str(cfg.get("stream_check_input", "2")).strip()
                base_name = REGION_NAMES.get(auto_input, f"模式 {auto_input}")
                region_desc = f"{base_name} (管理员固定设定)"
                logger.info(f"【{sname}】当前处于固定位置模式，跳过 API 探测，直接使用: {auto_input}")
            else:
                # 每次先尝试获取当前定位（较短超时时间）
                detected_country = await asyncio.to_thread(detect_ip_country_code, server_cfg)
                if detected_country and detected_country in COUNTRY_TO_REGION_INPUT:
                    # 获取成功 -> 写入缓存并使用
                    set_cached_country_code(sid, detected_country)
                    auto_input = COUNTRY_TO_REGION_INPUT[detected_country]
                    base_name = REGION_NAMES.get(auto_input, f"模式 {auto_input}")
                    region_desc = f"{base_name} (自动识别: {detected_country})"
                    logger.info(f"【{sname}】通过 IP.SB 自动识别出口 IP 国家: {detected_country} -> 流媒体选项: {auto_input}，已写入缓存")
                else:
                    # 获取超时或失败 -> 优先使用缓存
                    cached_country = get_cached_country_code(sid)
                    if cached_country and cached_country in COUNTRY_TO_REGION_INPUT:
                        auto_input = COUNTRY_TO_REGION_INPUT[cached_country]
                        base_name = REGION_NAMES.get(auto_input, f"模式 {auto_input}")
                        region_desc = f"{base_name} (定位超时，使用缓存: {cached_country})"
                        logger.warning(f"【{sname}】定位探测超时或未匹配 ({detected_country})，使用缓存国家: {cached_country} -> 流媒体选项: {auto_input}")
                    else:
                        # 首次超时且无缓存 -> 弹出方案4让用户手动选择，限时1分钟；若超时未选择，自动撤回消息并按方案2 (仅跨国平台: 0) 执行！
                        token = uuid.uuid4().hex[:8]
                        event = asyncio.Event()
                        selection_state = {
                            "event": event,
                            "selected_input": None,
                            "user_id": user_id,
                        }
                        _PENDING_STREAM_SELECTIONS[token] = selection_state

                        prefix = f"【{sname}】" if is_multi_server_mode() else ""
                        keyboard = [
                            [
                                InlineKeyboardButton("🇹🇼 台湾 (1)", callback_data=f"stream_sel:{sid}:{token}:1"),
                                InlineKeyboardButton("🇭🇰 香港 (2)", callback_data=f"stream_sel:{sid}:{token}:2"),
                                InlineKeyboardButton("🇯🇵 日本 (3)", callback_data=f"stream_sel:{sid}:{token}:3"),
                            ],
                            [
                                InlineKeyboardButton("🇺🇸 北美 (4)", callback_data=f"stream_sel:{sid}:{token}:4"),
                                InlineKeyboardButton("🇧🇷 南美 (5)", callback_data=f"stream_sel:{sid}:{token}:5"),
                                InlineKeyboardButton("🇪🇺 欧洲 (6)", callback_data=f"stream_sel:{sid}:{token}:6"),
                            ],
                            [
                                InlineKeyboardButton("🇦🇺 大洋洲 (7)", callback_data=f"stream_sel:{sid}:{token}:7"),
                                InlineKeyboardButton("🇰🇷 韩国 (8)", callback_data=f"stream_sel:{sid}:{token}:8"),
                                InlineKeyboardButton("🇸🇬 东南亚 (9)", callback_data=f"stream_sel:{sid}:{token}:9"),
                            ],
                            [
                                InlineKeyboardButton("🌐 仅跨国平台 (0)", callback_data=f"stream_sel:{sid}:{token}:0"),
                            ],
                        ]
                        prompt_text = (
                            f"⚠️ {prefix}未能自动探测到当前出口 IP 归属地，且本地无历史定位缓存。\n\n"
                            f"⏱️ 请在 1 分钟内点击下方按钮选择测试区域：\n"
                            f"（若 1 分钟内未选择，本提示将自动撤回，并按【仅跨国平台】直接执行）"
                        )
                        prompt_msg = await msg.reply_text(prompt_text, reply_markup=InlineKeyboardMarkup(keyboard))

                        try:
                            await asyncio.wait_for(event.wait(), timeout=60.0)
                            auto_input = selection_state.get("selected_input") or "0"
                            base_name = REGION_NAMES.get(auto_input, f"模式 {auto_input}")
                            region_desc = f"{base_name} (用户手动选择)"
                            try:
                                await prompt_msg.edit_text(f"已选择 [{region_desc}]，正在准备执行测试...", reply_markup=None)
                            except Exception:
                                pass
                            for cc, r_num in COUNTRY_TO_REGION_INPUT.items():
                                if r_num == auto_input:
                                    set_cached_country_code(sid, cc)
                                    break
                        except asyncio.TimeoutError:
                            # 超时未选择：自动撤回本选择消息，并按方案2 (0: 仅跨国平台) 执行！
                            try:
                                await prompt_msg.delete()
                            except Exception as e:
                                logger.debug(f"撤回超时流媒体选择消息失败: {e}")
                            auto_input = "0"
                            base_name = REGION_NAMES.get(auto_input, "仅跨国平台")
                            region_desc = f"{base_name} (选择超时未作答，自动按通用方案执行)"
                        finally:
                            _PENDING_STREAM_SELECTIONS.pop(token, None)

        prefix = f"【{sname}】" if is_multi_server_mode() else ""
        from utils.remote_ssh import get_ssh_config, is_remote_ssh_enabled
        if is_remote_ssh_enabled(server_cfg):
            ssh_cfg = get_ssh_config(server_config=server_cfg)
            status_text = (
                f"正在通过{prefix}远程家宽 SSH ({ssh_cfg['host']}) 执行流媒体解锁检测 [{region_desc}]，脚本将自动选择 {auto_input}，双栈耗时约 15~35 秒，请稍等..."
            )
        else:
            status_text = (
                f"正在{prefix}执行流媒体解锁检测 [{region_desc}]，脚本将自动选择 {auto_input}，请稍等..."
            )

        query = getattr(update, "callback_query", None)
        if query and query.message:
            try:
                await query.edit_message_text(status_text)
            except Exception:
                await msg.reply_text(status_text)
        else:
            await msg.reply_text(status_text)

        try:
            stream_cmd = str(cfg.get("stream_check_cmd") or DEFAULT_STREAM_CMD).strip()
            try:
                raw_timeout = int(cfg.get("stream_check_timeout", 1200))
            except (ValueError, TypeError):
                raw_timeout = 1200
            timeout = max(30, min(raw_timeout, 3600))
            return_code, output, elapsed = await asyncio.to_thread(
                run_stream_command,
                stream_cmd,
                auto_input,
                timeout,
                server_cfg,
            )
            stream_back_markup = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🎬 重新检测流媒体", callback_data="main_menu:stream"),
                    InlineKeyboardButton("🔙 返回主菜单", callback_data="main_menu:refresh"),
                ]
            ])
            summary_msg = prefix + build_stream_summary(return_code, output, elapsed)
            if query and query.message:
                try:
                    await query.edit_message_text(summary_msg, reply_markup=stream_back_markup)
                except Exception:
                    await msg.reply_text(summary_msg, reply_markup=stream_back_markup)
            else:
                await msg.reply_text(summary_msg, reply_markup=stream_back_markup)
        except subprocess.TimeoutExpired:
            err_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回主菜单", callback_data="main_menu:refresh")]])
            if query and query.message:
                try:
                    await query.edit_message_text(f"{prefix}流媒体检测超时，请稍后再试。", reply_markup=err_markup)
                except Exception:
                    await msg.reply_text(f"{prefix}流媒体检测超时，请稍后再试。", reply_markup=err_markup)
            else:
                await msg.reply_text(f"{prefix}流媒体检测超时，请稍后再试。")
        except Exception as e:
            logger.exception(f"【{sname}】流媒体检测失败: {e}")
            err_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回主菜单", callback_data="main_menu:refresh")]])
            if query and query.message:
                try:
                    await query.edit_message_text(f"{prefix}流媒体检测失败：{redact_text(str(e))}", reply_markup=err_markup)
                except Exception:
                    await msg.reply_text(f"{prefix}流媒体检测失败：{redact_text(str(e))}", reply_markup=err_markup)
            else:
                await msg.reply_text(f"{prefix}流媒体检测失败：{redact_text(str(e))}")
