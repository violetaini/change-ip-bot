import asyncio
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image
from telegram import Update
from telegram.ext import ContextTypes

from config import config
from handlers.user_check import check_user_permission
from utils.logger import logger
from utils.redact import redact_text

DEFAULT_QUALITY_CMD = "bash <(curl -sL https://IP.Check.Place) -y"
SVG_URL_RE = re.compile(r'https?://[^\s"\'<>]+\.svg(?:\?[^\s"\'<>]*)?', re.IGNORECASE)


def run_quality_command(cmd: str) -> tuple[int, str]:
    from utils.remote_ssh import is_remote_ssh_enabled, run_remote_ssh_command
    if is_remote_ssh_enabled():
        logger.info("通过远程家宽 SSH 执行 IP 质量检测脚本")
        return run_remote_ssh_command(cmd, timeout=900)

    run_kwargs = {
        "shell": True,
        "capture_output": True,
        "text": True,
        "timeout": 900,
    }
    bash_path = shutil.which("bash")
    if bash_path:
        run_kwargs["executable"] = bash_path
    elif "<(" in cmd:
        raise RuntimeError("当前 IP 质量检测命令需要 bash，但系统中未找到 bash")

    process = subprocess.run(
        cmd,
        **run_kwargs,
    )
    output = (process.stdout or "") + "\n" + (process.stderr or "")
    return process.returncode, output.strip()


def extract_svg_urls(text: str) -> list[str]:
    matches = SVG_URL_RE.findall(text or "")
    seen = set()
    result = []
    for u in matches:
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result


def extract_svg_url(text: str) -> str:
    urls = extract_svg_urls(text)
    return urls[0] if urls else ""


def find_browser_binary() -> str:
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        path = shutil.which(name)
        if path:
            return path
    return ""


def patch_svg_cjk_font(svg_bytes: bytes) -> bytes:
    """Ensure Chinese fonts take precedence over Latin-only fonts (like DejaVu Sans Mono)

    so CairoSVG renders Chinese characters correctly without tofu (□) boxes.
    """
    try:
        text = svg_bytes.decode("utf-8", errors="replace")
        if "font-family" in text:
            def replacer(match):
                original = match.group(1).strip()
                if any(k in original for k in ("WenQuanYi", "wqy", "Noto Sans Mono CJK", "Noto Sans CJK")):
                    return match.group(0)
                return f'font-family: "WenQuanYi Zen Hei Mono", "WenQuanYi Micro Hei Mono", "Noto Sans Mono CJK SC", {original}'

            text = re.sub(r'font-family:\s*([^;>]+)', replacer, text)
        return text.encode("utf-8")
    except Exception as e:
        logger.warning(f"修补SVG中文字体失败，使用原SVG: {e}")
        return svg_bytes


def render_svg_url_to_png(url: str, png_path: str) -> None:
    browser = find_browser_binary()
    if not browser:
        import cairosvg
        import requests

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "image/svg+xml,*/*",
        }
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            patched_content = patch_svg_cjk_font(resp.content)
            cairosvg.svg2png(bytestring=patched_content, write_to=png_path, output_width=1600)
        except Exception as e:
            logger.warning(f"通过网络请求并修补SVG渲染失败，尝试直接传递URL: {e}")
            cairosvg.svg2png(url=url, write_to=png_path, output_width=1600)

        if not os.path.exists(png_path):
            raise RuntimeError("CairoSVG 渲染失败，未生成 PNG 文件")
        return

    cmd = [
        browser,
        "--headless",
        "--disable-gpu",
        "--no-sandbox",
        "--hide-scrollbars",
        "--run-all-compositor-stages-before-draw",
        "--virtual-time-budget=5000",
        f"--screenshot={png_path}",
        "--window-size=1600,2400",
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0 or not os.path.exists(png_path):
        raise RuntimeError(
            f"浏览器截图失败: returncode={result.returncode}, stderr={(result.stderr or '').strip()}"
        )


def crop_report_area(png_path: str, jpg_path: str) -> None:
    with Image.open(png_path) as img:
        rgb = img.convert("RGB")
        width, height = rgb.size

        def color_bucket(pixel: tuple[int, int, int]) -> tuple[int, int, int]:
            return tuple(channel // 16 for channel in pixel)

        border_counts: dict[tuple[int, int, int], int] = {}
        border_samples: dict[tuple[int, int, int], list[tuple[int, int, int]]] = {}
        border_step = max(1, min(width, height) // 200)

        for x in range(0, width, border_step):
            for pixel in (rgb.getpixel((x, 0)), rgb.getpixel((x, height - 1))):
                bucket = color_bucket(pixel)
                border_counts[bucket] = border_counts.get(bucket, 0) + 1
                border_samples.setdefault(bucket, []).append(pixel)
        for y in range(0, height, border_step):
            for pixel in (rgb.getpixel((0, y)), rgb.getpixel((width - 1, y))):
                bucket = color_bucket(pixel)
                border_counts[bucket] = border_counts.get(bucket, 0) + 1
                border_samples.setdefault(bucket, []).append(pixel)

        bg_bucket = max(border_counts, key=border_counts.get)
        bg_pixels = border_samples[bg_bucket]
        bg = tuple(sum(px[i] for px in bg_pixels) // len(bg_pixels) for i in range(3))

        def is_foreground(pixel: tuple[int, int, int]) -> bool:
            return sum(abs(pixel[i] - bg[i]) for i in range(3)) > 40

        xs = []
        ys = []
        step = 2
        for y in range(0, height, step):
            for x in range(0, width, step):
                if is_foreground(rgb.getpixel((x, y))):
                    xs.append(x)
                    ys.append(y)

        if not xs or not ys:
            rgb.save(jpg_path, format="JPEG", quality=95)
            return

        left = max(0, min(xs) - 12)
        top = max(0, min(ys) - 12)
        right = min(width, max(xs) + 13)
        bottom = min(height, max(ys) + 13)

        cropped = rgb.crop((left, top, right, bottom))
        cropped.save(jpg_path, format="JPEG", quality=95)


async def ip_quality_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_permission(update):
        return

    if not config.get("ip_quality_enabled", True):
        await update.message.reply_text("IP 质量检测未启用。")
        return

    user_id = update.effective_user.id
    user_name = update.effective_user.username
    full_name = update.effective_user.full_name
    logger.info(f"收到 quality 命令，用户ID: {user_id}，用户名: {user_name}，全名: {full_name}")

    explicit_flag = ""
    if context.args:
        for arg in context.args:
            clean_arg = str(arg).strip().lower()
            if clean_arg in ("-4", "4", "ipv4", "v4"):
                explicit_flag = "-4"
                break
            elif clean_arg in ("-6", "6", "ipv6", "v6"):
                explicit_flag = "-6"
                break

    base_cmd = str(config.get("ip_quality_cmd") or DEFAULT_QUALITY_CMD).strip()
    quality_cmd = base_cmd
    if explicit_flag and explicit_flag not in quality_cmd:
        quality_cmd = f"{quality_cmd} {explicit_flag}"

    target_desc = "双栈 (IPv4 & IPv6)" if not explicit_flag else ("IPv6" if explicit_flag == "-6" else "IPv4")

    from utils.remote_ssh import is_remote_ssh_enabled, get_ssh_config
    if is_remote_ssh_enabled():
        cfg = get_ssh_config()
        await update.message.reply_text(f"正在通过远程家宽 SSH ({cfg['host']}) 检测 IP 质量 [{target_desc}]，双栈耗时可能需 1~3 分钟，完成后将发送报告预览...")
    else:
        await update.message.reply_text(f"正在检测 IP 质量 [{target_desc}]，完成后将发送图片预览...")

    loop = asyncio.get_running_loop()
    tmp_dir = None
    try:
        return_code, output = await loop.run_in_executor(None, run_quality_command, quality_cmd)
        logger.info(f"IP 质量检测命令返回码: {return_code}")

        svg_urls = extract_svg_urls(output)
        if not svg_urls:
            preview = output[-3000:] if output else "无输出"
            await update.message.reply_text(
                text="IP 质量检测完成，但没有识别到 SVG 链接。\n"
                     f"命令返回码：{return_code}\n\n"
                     f"最近输出：\n{redact_text(preview)}"
            )
            return

        tmp_dir = tempfile.mkdtemp(prefix="ip_quality_")
        successful_items = []
        failed_items = []

        total_reports = len(svg_urls)
        for idx, svg_url in enumerate(svg_urls):
            png_path = str(Path(tmp_dir) / f"report_{idx}.png")
            jpg_path = str(Path(tmp_dir) / f"report_{idx}.jpg")
            if explicit_flag == "-4":
                label = "IPv4"
            elif explicit_flag == "-6":
                label = "IPv6"
            elif total_reports >= 2:
                label = "IPv4" if idx == 0 else "IPv6" if idx == 1 else f"节点 {idx + 1}"
            else:
                label = "IPv4 / 单栈"

            try:
                await loop.run_in_executor(None, render_svg_url_to_png, svg_url, png_path)
                await loop.run_in_executor(None, crop_report_area, png_path, jpg_path)
                successful_items.append({"label": label, "jpg_path": jpg_path, "url": svg_url})
            except Exception as render_err:
                logger.warning(f"【{label}】IP质量图片渲染失败: {render_err}")
                failed_items.append({"label": label, "error": str(render_err), "url": svg_url})

        if failed_items and not successful_items:
            try:
                from utils.system_deps import ensure_system_dependencies
                loop.run_in_executor(None, ensure_system_dependencies)
            except Exception:
                pass

        if len(successful_items) == 1:
            item = successful_items[0]
            with open(item["jpg_path"], "rb") as f:
                await update.message.reply_photo(
                    photo=f,
                    caption=f"【{item['label']}】IP 质量检测完成，图片预览已附上。\n🔗 原始报告: {item['url']}",
                )
        elif len(successful_items) > 1:
            from telegram import InputMediaPhoto
            media = []
            files_to_close = []
            try:
                for item in successful_items:
                    f = open(item["jpg_path"], "rb")
                    files_to_close.append(f)
                    media.append(InputMediaPhoto(
                        media=f,
                        caption=f"【{item['label']}】IP 质量检测报告\n🔗 原始报告: {item['url']}",
                    ))
                await update.message.reply_media_group(media=media)
            except Exception as mg_err:
                logger.warning(f"发送媒体组失败，降级为逐张发送: {mg_err}")
                for item in successful_items:
                    with open(item["jpg_path"], "rb") as f:
                        await update.message.reply_photo(
                            photo=f,
                            caption=f"【{item['label']}】IP 质量检测报告\n🔗 原始报告: {item['url']}",
                        )
            finally:
                for f in files_to_close:
                    try:
                        f.close()
                    except Exception:
                        pass

        for item in failed_items:
            await update.message.reply_text(
                f"【{item['label']}】图片渲染失败（{redact_text(item['error'])}），已自动降级为报告链接：\n🔗 {item['url']}"
            )
    except subprocess.TimeoutExpired:
        await update.message.reply_text("IP 质量检测超时，请稍后再试。")
    except Exception as e:
        logger.exception(f"IP 质量检测失败: {e}")
        await update.message.reply_text(f"IP 质量检测失败：{redact_text(str(e))}")
    finally:
        if tmp_dir and os.path.isdir(tmp_dir):
            try:
                for p in Path(tmp_dir).glob("*"):
                    try:
                        p.unlink(missing_ok=True)
                    except TypeError:
                        if p.exists():
                            p.unlink()
                os.rmdir(tmp_dir)
            except Exception as cleanup_err:
                logger.warning(f"清理 IP 质量临时文件失败: {cleanup_err}")
