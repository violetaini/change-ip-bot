import json
import os
import re
import shutil
import subprocess
import time
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


_mainland_target_cache: Dict[str, Any] = {"v4": "", "v6": "", "expires_at": 0.0}


def resolve_mainland_target(
    domain: str = "v.qq.com",
    ecs_subnet: str = "202.96.128.86/24",
    timeout: int = 5,
) -> Tuple[str, str]:
    """
    使用 EDNS Client Subnet (ECS) 通过公共 DoH 服务解析国内直连目标（默认 v.qq.com 匹配广东电信网段）。
    确保海外/香港节点解析出的目标 IP 真实位于中国境内，而不是海外 Anycast 节点。
    带 10 分钟本地缓存与预置兜底 IP。
    """
    global _mainland_target_cache
    now = time.time()
    if now < _mainland_target_cache.get("expires_at", 0) and _mainland_target_cache.get("v4"):
        return _mainland_target_cache["v4"], _mainland_target_cache.get("v6", "")

    v4_target = ""
    v6_target = ""

    doh_configs = [
        (
            f"https://dns.alidns.com/resolve?name={domain}&type=A&edns_client_subnet={ecs_subnet}",
            f"https://dns.alidns.com/resolve?name={domain}&type=AAAA&edns_client_subnet={ecs_subnet}",
        ),
        (
            f"https://dns.google/resolve?name={domain}&type=A&edns_client_subnet={ecs_subnet}",
            f"https://dns.google/resolve?name={domain}&type=AAAA&edns_client_subnet={ecs_subnet}",
        ),
    ]

    for u_v4, u_v6 in doh_configs:
        try:
            if not v4_target:
                r4 = requests.get(u_v4, timeout=timeout).json()
                for ans in r4.get("Answer", []):
                    if ans.get("type") == 1 and ans.get("data"):
                        data_str = ans["data"].strip()
                        if is_valid_ipv4(data_str):
                            v4_target = data_str
                            break
            if not v6_target:
                r6 = requests.get(u_v6, timeout=timeout).json()
                for ans in r6.get("Answer", []):
                    if ans.get("type") == 28 and ans.get("data"):
                        data_str = ans["data"].strip()
                        if ":" in data_str:
                            v6_target = data_str
                            break
        except Exception as ex:
            logger.debug(f"DoH 解析 {domain} 异常: {ex}")

        if v4_target and v6_target:
            break

    if not v4_target:
        v4_target = "219.144.82.193"
    if not v6_target:
        v6_target = "240e:ab:b202:1d:70::1c"

    _mainland_target_cache = {
        "v4": v4_target,
        "v6": v6_target,
        "expires_at": now + 600.0,
    }
    return v4_target, v6_target


def probe_domestic_http(
    target_ip: str,
    ip_version: int,
    domain: str = "v.qq.com",
    retries: int = 3,
    timeout: int = 4,
    run_ssh_fn=None,
) -> Tuple[bool, str, int]:
    """
    向国内目标 IP 发起 HTTPS 连接探测，验证 TCP 握手 + TLS + HTTP 返回。
    如果单次失败，自动按用户要求重试指定次数（默认 3 次），避免网络抖动误判。
    """
    if not target_ip:
        return False, "目标IP为空", 0

    cmd_flag = "-6" if ip_version == 6 else ""
    ip_fmt = f"[{target_ip}]" if ip_version == 6 else target_ip
    curl_cmd = (
        f"curl {cmd_flag} -s -I --connect-timeout {timeout} -m {timeout + 2} "
        f"--resolve {domain}:443:{ip_fmt} https://{domain} -o /dev/null "
        f'-w "%{{http_code}}|%{{time_connect}}"'
    )

    for attempt in range(1, retries + 1):
        try:
            if run_ssh_fn:
                code, out = run_ssh_fn(curl_cmd, timeout=timeout + 5)
            else:
                res = subprocess.run(
                    curl_cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=timeout + 5,
                )
                code, out = res.returncode, res.stdout

            parts = out.strip().split("|")
            if code == 0 and len(parts) == 2 and parts[0] in ["200", "301", "302", "403", "404"]:
                try:
                    connect_sec = float(parts[1])
                    ms = max(1, int(connect_sec * 1000))
                except (ValueError, TypeError):
                    ms = 0
                return True, f"正常 (HTTP {parts[0]}, 握手 {ms}ms)", ms
        except Exception as ex:
            logger.debug(f"探测 {target_ip} 第 {attempt} 次失败: {ex}")

        if attempt < retries:
            time.sleep(0.5)

    return False, f"超时/丢包 (重试{retries}次均失败)", 0


def check_ip_blocked() -> Tuple[bool, str]:
    ip = get_current_ip()
    try:
        v4_target, _ = resolve_mainland_target()
        ok, desc, _ = probe_domestic_http(v4_target, 4, retries=3, timeout=4)
        return (not ok), ip
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

