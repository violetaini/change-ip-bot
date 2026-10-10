import asyncio
import json
import re
import shutil
import subprocess
import time
from typing import Any, Dict, Optional

import requests
from telegram import Update
from telegram.ext import ContextTypes

from config import config, is_multi_server_mode
from handlers.server_selection import resolve_target_server
from handlers.user_check import check_user_permission
from utils.logger import logger
from utils.redact import redact_text


DEFAULT_STREAM_CMD = "bash <(curl -L -s https://github.com/1-stream/RegionRestrictionCheck/raw/main/check.sh)"
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
        code, out = run_remote_ssh_command(cmd, timeout=max(30, int(timeout)), input_data=f"{auto_input.rstrip()}\n", server_config=server_config)
        elapsed = time.monotonic() - started
        return code, strip_ansi(out).strip(), elapsed

    run_kwargs = {
        "shell": True,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": max(30, int(timeout)),
        "input": f"{auto_input.rstrip()}\n",
    }
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


def detect_ip_country_code(server_config: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Detect exit IP country code via IP.SB API (remotely via SSH if configured, else locally)."""
    from utils.remote_ssh import is_remote_ssh_enabled, run_remote_ssh_command

    cfg = server_config if server_config is not None else config
    url = str(cfg.get("stream_check_api") or DEFAULT_STREAM_API).strip()
    try:
        if is_remote_ssh_enabled(server_config):
            cmd = f'curl -s -m 5 "{url}"'
            code, out = run_remote_ssh_command(cmd, timeout=10, server_config=server_config)
            if code == 0 and out:
                return parse_country_code_from_response(out)
        else:
            resp = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                timeout=5,
            )
            if resp.ok:
                return parse_country_code_from_response(resp.text)
    except Exception as e:
        logger.warning(f"自动检测出口 IP 归属地失败 ({url}): {e}")
    return None


async def stream_check_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_permission(update):
        return

    if not config.get("stream_check_enabled", True):
        msg = update.message or (update.callback_query.message if update.callback_query else None)
        if msg:
            await msg.reply_text("流媒体检测未启用。")
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
        if auto_detect:
            detected_country = await asyncio.to_thread(detect_ip_country_code, server_cfg)
            if detected_country and detected_country in COUNTRY_TO_REGION_INPUT:
                auto_input = COUNTRY_TO_REGION_INPUT[detected_country]
                base_name = REGION_NAMES.get(auto_input, f"模式 {auto_input}")
                region_desc = f"{base_name} (自动识别: {detected_country})"
                logger.info(f"【{sname}】通过 IP.SB 自动识别出口 IP 国家: {detected_country} -> 流媒体选项: {auto_input}")
            else:
                auto_input = str(cfg.get("stream_check_input", "2")).strip()
                base_name = REGION_NAMES.get(auto_input, f"模式 {auto_input}")
                reason = f"自动识别: {detected_country} 未匹配，使用默认" if detected_country else "识别超时/失败，使用默认"
                region_desc = f"{base_name} ({reason})"
                logger.info(f"【{sname}】国家代码未匹配或识别失败 ({detected_country})，回退到默认选项: {auto_input}")
        else:
            auto_input = str(cfg.get("stream_check_input", "2")).strip()
            base_name = REGION_NAMES.get(auto_input, f"模式 {auto_input}")
            region_desc = f"{base_name} (默认)"

    prefix = f"【{sname}】" if is_multi_server_mode() else ""
    from utils.remote_ssh import get_ssh_config, is_remote_ssh_enabled
    if is_remote_ssh_enabled(server_cfg):
        ssh_cfg = get_ssh_config(server_config=server_cfg)
        await msg.reply_text(
            f"正在通过{prefix}远程家宽 SSH ({ssh_cfg['host']}) 执行流媒体解锁检测 [{region_desc}]，脚本将自动选择 {auto_input}，双栈耗时约 15~35 秒，请稍等..."
        )
    else:
        await msg.reply_text(
            f"正在{prefix}执行流媒体解锁检测 [{region_desc}]，脚本将自动选择 {auto_input}，请稍等..."
        )

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
        logger.info(f"【{sname}】流媒体检测命令返回码: {return_code}，输出长度: {len(output or '')}")
        await msg.reply_text(prefix + build_stream_summary(return_code, output, elapsed))
    except subprocess.TimeoutExpired:
        await msg.reply_text(f"{prefix}流媒体检测超时，请稍后再试。")
    except Exception as e:
        logger.exception(f"【{sname}】流媒体检测失败: {e}")
        await msg.reply_text(f"{prefix}流媒体检测失败：{redact_text(str(e))}")
