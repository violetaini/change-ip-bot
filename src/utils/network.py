import json
import os
import re
import shutil
import subprocess
from typing import Any, Dict, Tuple

import requests

from config import config
from utils.logger import logger
from utils.redact import redact_text

IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


class ChangeIPTimeoutError(Exception):
    pass


def is_valid_ipv4(ip: str) -> bool:
    if not ip or not IPV4_RE.match(ip):
        return False
    parts = ip.split(".")
    return all(0 <= int(p) <= 255 for p in parts)


def get_current_ip() -> str:
    timeout = int(config.get("ip_check_timeout", 60))
    try:
        if config.get("ip_check_api"):
            response = requests.get(config["ip_check_api"], timeout=timeout)
            response.raise_for_status()
            ip = response.text.strip()
        else:
            cmd = config["ip_check_cmd"]
            run_kwargs = {
                "shell": True,
                "capture_output": True,
                "text": True,
                "timeout": timeout,
            }
            bash_path = shutil.which("bash")
            if os.name != "nt" and bash_path:
                run_kwargs["executable"] = bash_path
            result = subprocess.run(
                cmd,
                **run_kwargs,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "命令执行失败")
            ip = result.stdout.strip()

        if not ip:
            raise RuntimeError("获取到的IP为空")
        return ip
    except Exception as e:
        logger.error(f"获取IP地址失败: {e}")
        raise


def check_ip_blocked() -> Tuple[bool, str]:
    ip = get_current_ip()
    try:
        if os.name == "nt":
            cmd = ["ping", "-n", "5", "-w", "2000", "www.itdog.cn"]
        else:
            cmd = ["ping", "-c", "5", "-W", "2", "-i", "0.2", "www.itdog.cn"]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = f"{result.stdout}\n{result.stderr}"
        is_blocked = "100% packet loss" in output or "100% loss" in output
        return is_blocked, ip
    except Exception as e:
        logger.error(f"检查IP状态失败: {e}")
        raise


def call_change_ip_api(api_url: str, timeout: int = 600) -> Dict[str, Any]:
    try:
        response = requests.get(api_url, timeout=timeout)
        response.raise_for_status()
    except requests.exceptions.ReadTimeout as e:
        raise ChangeIPTimeoutError(f"换IP接口读取超时（{timeout}秒）") from e
    except requests.exceptions.ConnectTimeout as e:
        raise ChangeIPTimeoutError(f"换IP接口连接超时（{timeout}秒）") from e
    except requests.exceptions.Timeout as e:
        raise ChangeIPTimeoutError(f"换IP接口超时（{timeout}秒）") from e
    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code if e.response is not None else "未知"
        raise RuntimeError(f"换IP API HTTP错误: {status_code}") from e
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"换IP API请求失败: {e.__class__.__name__}") from e

    try:
        data = response.json()
    except json.JSONDecodeError:
        raise RuntimeError(f"换IP API 返回的不是JSON: {response.text.strip()}")

    if not isinstance(data, dict):
        raise RuntimeError(f"换IP API 返回格式异常: {data}")

    logger.info(f"换IP API 响应: {redact_text(str(data))}")
    return data


def parse_change_ip_result(data: Dict[str, Any]) -> Dict[str, Any]:
    status = str(data.get("status", "")).strip()
    old_ip = str(data.get("old_ip", "")).strip()
    new_ip = str(data.get("new_ip", "")).strip()

    success = (status == "IP changed") and is_valid_ipv4(new_ip)
    unchanged = status == "IP unchanged"

    return {
        "success": success,
        "unchanged": unchanged,
        "status": status,
        "old_ip": old_ip,
        "new_ip": new_ip,
        "raw": data,
    }


def verify_public_ip_matches(target_ip: str) -> bool:
    if not target_ip:
        return False
    try:
        current_ip = get_current_ip()
        return current_ip == target_ip
    except Exception:
        return False


def call_boil_change_ip(base_url: str, token: str, timeout: int = 30) -> Dict[str, Any]:
    clean_base = str(base_url or "https://ippanel.boil.network").strip().rstrip("/")
    url = f"{clean_base}/api/v1/changeIP"
    headers = {
        "Authorization": f"Bearer {str(token or '').strip()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        response = requests.post(url, headers=headers, timeout=timeout)
    except requests.exceptions.ReadTimeout as e:
        raise ChangeIPTimeoutError(f"Boil API 读取超时（{timeout}秒）") from e
    except requests.exceptions.ConnectTimeout as e:
        raise ChangeIPTimeoutError(f"Boil API 连接超时（{timeout}秒）") from e
    except requests.exceptions.Timeout as e:
        raise ChangeIPTimeoutError(f"Boil API 超时（{timeout}秒）") from e
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Boil API 请求失败: {e.__class__.__name__}") from e

    try:
        data = response.json()
    except json.JSONDecodeError:
        raise RuntimeError(f"Boil API 未返回有效JSON: {response.text.strip()[:200]}")

    if not isinstance(data, dict):
        raise RuntimeError(f"Boil API 返回格式异常: {data}")

    logger.info(f"Boil 换IP API 响应 (HTTP {response.status_code}): {redact_text(str(data))}")

    if response.status_code in (400, 405):
        err_msg = str(data.get("error") or data.get("message") or f"HTTP {response.status_code}")
        raise RuntimeError(f"Boil 接口错误: {err_msg}")

    if not response.ok:
        raise RuntimeError(f"Boil API HTTP错误 {response.status_code}: {data}")

    return data


def call_boil_get_ip(base_url: str, token: str, timeout: int = 30) -> str:
    clean_base = str(base_url or "https://ippanel.boil.network").strip().rstrip("/")
    url = f"{clean_base}/api/v1/getIP"
    headers = {
        "Authorization": f"Bearer {str(token or '').strip()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        response = requests.post(url, headers=headers, timeout=timeout)
    except Exception as e:
        raise RuntimeError(f"Boil 获取IP请求失败: {e}") from e

    try:
        data = response.json()
    except json.JSONDecodeError:
        raise RuntimeError(f"Boil 获取IP未返回有效JSON: {response.text.strip()[:200]}")

    if response.status_code in (400, 405):
        err_msg = str(data.get("error") or f"HTTP {response.status_code}")
        raise RuntimeError(f"Boil 获取IP失败: {err_msg}")

    if not response.ok or not data.get("ok"):
        raise RuntimeError(f"Boil 获取IP异常: {data}")

    ip = str(data.get("ip", "")).strip()
    if not ip or not is_valid_ipv4(ip):
        raise RuntimeError(f"Boil 返回的IP无效: {ip}")

    return ip

