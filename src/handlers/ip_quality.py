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

DEFAULT_QUALITY_CMD = "bash <(curl -sL https://IP.Check.Place) -y"
SVG_URL_RE = re.compile(r'https?://[^\s"\'<>]+\.svg(?:\?[^\s"\'<>]*)?', re.IGNORECASE)


def run_quality_command(cmd: str) -> tuple[int, str]:
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


def extract_svg_url(text: str) -> str:
    match = SVG_URL_RE.search(text)
    return match.group(0) if match else ""


def find_browser_binary() -> str:
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        path = shutil.which(name)
        if path:
            return path
    return ""


def render_svg_url_to_png(url: str, png_path: str) -> None:
    browser = find_browser_binary()
    if not browser:
        import cairosvg

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

        corners = [
            rgb.getpixel((0, 0)),
            rgb.getpixel((width - 1, 0)),
            rgb.getpixel((0, height - 1)),
            rgb.getpixel((width - 1, height - 1)),
        ]
        bg = tuple(sum(px[i] for px in corners) // len(corners) for i in range(3))

        def is_foreground(pixel: tuple[int, int, int]) -> bool:
            return sum(abs(pixel[i] - bg[i]) for i in range(3)) > 25

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
        right = min(width, max(xs) + 12)
        bottom = min(height, max(ys) + 12)

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

    await update.message.reply_text("正在检测 IP 质量，完成后将直接发送裁切后的图片预览...")

    loop = asyncio.get_running_loop()
    tmp_dir = None
    try:
        quality_cmd = str(config.get("ip_quality_cmd") or DEFAULT_QUALITY_CMD).strip()
        return_code, output = await loop.run_in_executor(None, run_quality_command, quality_cmd)
        logger.info(f"IP 质量检测命令返回码: {return_code}")

        svg_url = extract_svg_url(output)
        if not svg_url:
            preview = output[-3000:] if output else "无输出"
            await update.message.reply_text(
                text="IP 质量检测完成，但没有识别到 SVG 链接。\n"
                     f"命令返回码：{return_code}\n\n"
                     f"最近输出：\n{preview}"
            )
            return

        tmp_dir = tempfile.mkdtemp(prefix="ip_quality_")
        png_path = str(Path(tmp_dir) / "ip_quality_report.png")
        jpg_path = str(Path(tmp_dir) / "ip_quality_report.jpg")

        await loop.run_in_executor(None, render_svg_url_to_png, svg_url, png_path)
        await loop.run_in_executor(None, crop_report_area, png_path, jpg_path)

        with open(jpg_path, "rb") as f:
            await update.message.reply_photo(
                photo=f,
                caption="IP 质量检测完成，图片预览已附上。",
            )
    except subprocess.TimeoutExpired:
        await update.message.reply_text("IP 质量检测超时，请稍后再试。")
    except Exception as e:
        logger.exception(f"IP 质量检测失败: {e}")
        await update.message.reply_text(f"IP 质量检测失败：{e}")
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
