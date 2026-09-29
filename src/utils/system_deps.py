import os
import platform
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Tuple

from utils.logger import logger

_AUTO_INSTALL_ATTEMPTED = False


def is_root_user() -> bool:
    if hasattr(os, "geteuid"):
        return os.geteuid() == 0
    return False


def check_cairo_available() -> Tuple[bool, str]:
    # Check Chromium first
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        if shutil.which(name):
            return True, f"Chromium ({name})"

    try:
        import cairosvg
        # Try a dummy conversion to ensure ctypes/cffi successfully loads libcairo
        cairosvg.svg2png(bytestring=b"<svg width=\"10\" height=\"10\" xmlns=\"http://www.w3.org/2000/svg\"><rect width=\"10\" height=\"10\"/></svg>")
        return True, "CairoSVG"
    except Exception as e:
        return False, str(e)


def check_cjk_font_available() -> Tuple[bool, str]:
    if platform.system() != "Linux":
        return True, "非 Linux 系统，使用系统默认字体"

    # Check fontconfig
    if shutil.which("fc-list"):
        try:
            res = subprocess.run(["fc-list", ":lang=zh"], capture_output=True, text=True, timeout=5)
            if res.returncode == 0 and res.stdout.strip():
                return True, "已安装中文字体"
        except Exception:
            pass

    # Check font directories
    font_paths = [
        "/usr/share/fonts/truetype/wqy",
        "/usr/share/fonts/opentype/noto",
        "/usr/share/fonts/truetype/noto",
        "/usr/share/fonts/wqy-zenhei",
        "/usr/share/fonts/cjk",
    ]
    for p in font_paths:
        if os.path.isdir(p) and os.listdir(p):
            return True, f"已找到字体目录: {p}"

    return False, "未找到常用中文字体（如 wqy-zenhei / noto-cjk）"


def check_tool_available(tool_name: str) -> bool:
    return bool(shutil.which(tool_name))


def get_system_dependency_status() -> Dict[str, Any]:
    cairo_ok, cairo_msg = check_cairo_available()
    font_ok, font_msg = check_cjk_font_available()
    curl_ok = check_tool_available("curl")
    bash_ok = check_tool_available("bash")
    speedtest_ok = check_tool_available("speedtest")

    return {
        "cairo": {"ok": cairo_ok, "detail": cairo_msg},
        "font": {"ok": font_ok, "detail": font_msg},
        "curl": {"ok": curl_ok, "detail": "可用" if curl_ok else "未安装"},
        "bash": {"ok": bash_ok, "detail": "可用" if bash_ok else "未安装"},
        "speedtest": {"ok": speedtest_ok, "detail": "可用" if speedtest_ok else "未安装"},
        "is_root": is_root_user(),
        "os": platform.system(),
    }


def ensure_system_dependencies() -> Dict[str, Any]:
    global _AUTO_INSTALL_ATTEMPTED
    if _AUTO_INSTALL_ATTEMPTED:
        return {"status": "already_attempted"}
    _AUTO_INSTALL_ATTEMPTED = True

    if platform.system() != "Linux":
        logger.info("非 Linux 环境，跳过系统包自检与安装")
        return {"status": "skipped", "reason": "non-linux"}

    status = get_system_dependency_status()
    missing_cairo = not status["cairo"]["ok"]
    missing_font = not status["font"]["ok"]
    missing_curl = not status["curl"]["ok"]

    if not (missing_cairo or missing_font or missing_curl):
        logger.info("系统依赖自检通过，基础依赖（Cairo/中文字体/curl）均已就绪")
        return {"status": "ok", "missing": []}

    if not is_root_user():
        msg = "检测到缺失系统依赖，但当前非 root 权限，无法自动执行 apt/包管理器安装"
        logger.warning(msg)
        return {"status": "skipped", "reason": "not_root"}

    # Detect package manager
    pkg_mgr = ""
    for mgr in ("apt-get", "dnf", "yum", "apk", "pacman"):
        if shutil.which(mgr):
            pkg_mgr = mgr
            break

    if not pkg_mgr:
        logger.warning("未检测到支持的包管理器（apt-get/dnf/yum/apk/pacman），跳过自动安装")
        return {"status": "skipped", "reason": "no_package_manager"}

    pkgs_to_install: List[str] = []
    if pkg_mgr == "apt-get":
        if missing_cairo:
            pkgs_to_install.append("libcairo2")
        if missing_font:
            pkgs_to_install.extend(["fonts-wqy-zenhei", "fonts-wqy-microhei"])
        if missing_curl:
            pkgs_to_install.append("curl")
        cmd = [
            "apt-get", "update", "-qq"
        ]
        install_cmd = [
            "apt-get", "install", "-y", "--no-install-recommends"
        ] + pkgs_to_install
    elif pkg_mgr in ("dnf", "yum"):
        if missing_cairo:
            pkgs_to_install.append("cairo")
        if missing_font:
            pkgs_to_install.append("wqy-zenhei-fonts")
        if missing_curl:
            pkgs_to_install.append("curl")
        cmd = []
        install_cmd = [pkg_mgr, "install", "-y"] + pkgs_to_install
    elif pkg_mgr == "apk":
        if missing_cairo:
            pkgs_to_install.append("cairo")
        if missing_font:
            pkgs_to_install.append("font-noto-cjk")
        if missing_curl:
            pkgs_to_install.append("curl")
        cmd = []
        install_cmd = ["apk", "add", "--no-cache"] + pkgs_to_install
    elif pkg_mgr == "pacman":
        if missing_cairo:
            pkgs_to_install.append("cairo")
        if missing_font:
            pkgs_to_install.append("wqy-zenhei")
        if missing_curl:
            pkgs_to_install.append("curl")
        cmd = []
        install_cmd = ["pacman", "-S", "--noconfirm"] + pkgs_to_install
    else:
        return {"status": "skipped", "reason": "unsupported_package_manager"}

    if not pkgs_to_install:
        return {"status": "ok", "missing": []}

    logger.info(f"检测到缺失依赖: {pkgs_to_install}，使用 {pkg_mgr} 自动安装中...")
    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"

    try:
        if cmd:
            subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
        res = subprocess.run(install_cmd, env=env, capture_output=True, text=True, timeout=120)
        if res.returncode == 0:
            logger.info(f"自动安装系统依赖成功: {pkgs_to_install}")
            return {"status": "installed", "packages": pkgs_to_install}
        else:
            logger.error(f"自动安装系统依赖失败 (code {res.returncode}): {res.stderr}")
            return {"status": "failed", "packages": pkgs_to_install, "error": res.stderr}
    except Exception as e:
        logger.exception(f"执行自动安装依赖异常: {e}")
        return {"status": "error", "packages": pkgs_to_install, "error": str(e)}
