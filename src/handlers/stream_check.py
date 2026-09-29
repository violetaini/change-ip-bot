import asyncio
import re
import shutil
import subprocess
import time

from telegram import Update
from telegram.ext import ContextTypes

from config import config
from handlers.user_check import check_user_permission
from utils.logger import logger
from utils.redact import redact_text


DEFAULT_STREAM_CMD = "bash <(curl -L -s https://github.com/1-stream/RegionRestrictionCheck/raw/main/check.sh)"
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


def run_stream_command(cmd: str, auto_input: str, timeout: int) -> tuple[int, str, float]:
    from utils.remote_ssh import is_remote_ssh_enabled, run_remote_ssh_command
    if is_remote_ssh_enabled():
        logger.info("通过远程家宽 SSH 执行流媒体解锁检测脚本")
        started = time.monotonic()
        code, out = run_remote_ssh_command(cmd, timeout=max(30, int(timeout)), input_data=f"{auto_input.rstrip()}\n")
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


async def stream_check_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_permission(update):
        return

    if not config.get("stream_check_enabled", True):
        await update.message.reply_text("流媒体检测未启用。")
        return

    logger.info(
        f"收到 stream 命令，用户ID: {update.effective_user.id}，"
        f"用户名: {update.effective_user.username}"
    )

    auto_input = str(config.get("stream_check_input", "2")).strip()
    if context.args:
        first_arg = str(context.args[0]).strip()
        if first_arg in REGION_NAMES or first_arg.isdigit():
            auto_input = first_arg

    region_desc = REGION_NAMES.get(auto_input, f"模式 {auto_input}")

    from utils.remote_ssh import is_remote_ssh_enabled, get_ssh_config
    if is_remote_ssh_enabled():
        cfg = get_ssh_config()
        await update.message.reply_text(
            f"正在通过远程家宽 SSH ({cfg['host']}) 执行流媒体解锁检测 [{region_desc}]，脚本将自动选择 {auto_input}，双栈耗时约 15~35 秒，请稍等..."
        )
    else:
        await update.message.reply_text(
            f"正在执行流媒体解锁检测 [{region_desc}]，脚本将自动选择 {auto_input}，请稍等..."
        )

    try:
        stream_cmd = str(config.get("stream_check_cmd") or DEFAULT_STREAM_CMD).strip()
        timeout = int(config.get("stream_check_timeout", 1200))
        return_code, output, elapsed = await asyncio.to_thread(
            run_stream_command,
            stream_cmd,
            auto_input,
            timeout,
        )
        logger.info(f"流媒体检测命令返回码: {return_code}，输出长度: {len(output or '')}")
        await update.message.reply_text(build_stream_summary(return_code, output, elapsed))
    except subprocess.TimeoutExpired:
        await update.message.reply_text("流媒体检测超时，请稍后再试。")
    except Exception as e:
        logger.exception(f"流媒体检测失败: {e}")
        await update.message.reply_text(f"流媒体检测失败：{redact_text(str(e))}")
