import os
import re
import shutil
import subprocess
import time
from typing import Any, Dict, Optional, Tuple

import requests

from config import config
from utils.logger import logger
from utils.redact import redact_text

_DNS_LOCAL_CACHE: Dict[str, str] = {}


def set_cached_host_ip(hostname: str, ip: str) -> None:
    clean_host = str(hostname or "").strip().rstrip(".").lower()
    clean_ip = str(ip or "").strip()
    if clean_host and clean_ip:
        _DNS_LOCAL_CACHE[clean_host] = clean_ip
        logger.info(f"本地DNS映射更新: {clean_host} -> {clean_ip}")


def get_cached_host_ip(hostname: str) -> str:
    clean_host = str(hostname or "").strip().rstrip(".").lower()
    return _DNS_LOCAL_CACHE.get(clean_host, "")


def resolve_via_cloudflare_doh(hostname: str, timeout: int = 5) -> str:
    """Resolve A record using Cloudflare 1.1.1.1 DNS over HTTPS directly to bypass any local DNS cache (SmartDNS/AdGuard)."""
    clean_host = str(hostname or "").strip().rstrip(".")
    if not clean_host:
        return ""

    url = f"https://1.1.1.1/dns-query?name={clean_host}&type=A"
    headers = {"Accept": "application/dns-json"}
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        if resp.ok:
            data = resp.json()
            for ans in data.get("Answer", []):
                if ans.get("type") == 1 and ans.get("data"):
                    ip = str(ans["data"]).strip()
                    if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip):
                        return ip
    except Exception as e:
        logger.debug(f"1.1.1.1 DoH 解析失败 ({clean_host}): {e}")
    return ""


def resolve_target_host(hostname: str) -> str:
    clean_host = str(hostname or "").strip().rstrip(".")
    if not clean_host:
        return ""

    # 1. Already an IPv4
    if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", clean_host):
        return clean_host

    # 2. Check local memory cache
    cached = get_cached_host_ip(clean_host)
    if cached:
        return cached

    # 3. If Boil mode, try querying live Boil API
    try:
        from utils.network import call_boil_get_ip
        provider = str(config.get("ip_change_provider", "classic")).strip().lower()
        if provider == "boil":
            token = str(config.get("boil_api_token", "")).strip()
            base_url = str(config.get("boil_api_base_url", "https://ippanel.boil.network")).strip()
            if token:
                live_ip = call_boil_get_ip(base_url, token, timeout=5)
                if live_ip:
                    set_cached_host_ip(clean_host, live_ip)
                    return live_ip
    except Exception:
        pass

    # 4. Query Cloudflare 1.1.1.1 directly
    doh_ip = resolve_via_cloudflare_doh(clean_host, timeout=5)
    if doh_ip:
        set_cached_host_ip(clean_host, doh_ip)
        return doh_ip

    return clean_host


def is_remote_ssh_enabled() -> bool:
    return bool(config.get("remote_ssh_enabled", False))


def get_ssh_config() -> Dict[str, Any]:
    raw_host = str(config.get("remote_ssh_host") or "").strip()
    target_ip = resolve_target_host(raw_host)

    return {
        "raw_host": raw_host,
        "host": target_ip,
        "port": int(config.get("remote_ssh_port") or 22),
        "user": str(config.get("remote_ssh_user") or "root").strip(),
        "key_path": str(config.get("remote_ssh_key_path") or "").strip(),
        "password": str(config.get("remote_ssh_password") or "").strip(),
        "timeout": int(config.get("remote_ssh_timeout") or 300),
    }


def build_ssh_command_prefix(ssh_cfg: Dict[str, Any]) -> list[str]:
    cmd = [
        "ssh",
        "-p", str(ssh_cfg["port"]),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=15",
        "-o", "LogLevel=ERROR",
    ]

    key_path = ssh_cfg.get("key_path")
    if key_path and os.path.exists(key_path):
        cmd.extend(["-i", key_path])

    user = ssh_cfg.get("user") or "root"
    host = ssh_cfg.get("host")
    cmd.append(f"{user}@{host}")
    return cmd


def run_remote_ssh_command(
    command_str: str,
    timeout: int = 300,
    input_data: Optional[str] = None,
) -> Tuple[int, str]:
    ssh_cfg = get_ssh_config()
    prefix = build_ssh_command_prefix(ssh_cfg)
    full_cmd = prefix + [command_str]

    logger.info(f"执行远程家宽 SSH 命令 ({ssh_cfg['user']}@{ssh_cfg['host']}:{ssh_cfg['port']}): {redact_text(command_str[:200])}")

    run_kwargs = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": max(10, int(timeout)),
    }
    if input_data is not None:
        run_kwargs["input"] = input_data

    try:
        res = subprocess.run(full_cmd, **run_kwargs)
        output = (res.stdout or "") + ("\n" + res.stderr if res.stderr else "")
        return res.returncode, output.strip()
    except subprocess.TimeoutExpired as e:
        msg = f"远程 SSH 执行超时（{timeout}秒，目标: {ssh_cfg['host']}:{ssh_cfg['port']}）"
        logger.warning(msg)
        raise TimeoutError(msg) from e
    except Exception as e:
        msg = f"远程 SSH 执行异常: {redact_text(str(e))}"
        logger.error(msg)
        raise RuntimeError(msg) from e


def test_remote_ssh_connectivity() -> Tuple[bool, str, float]:
    """Test SSH connectivity and return (success, resolved_ip, rtt_ms)."""
    ssh_cfg = get_ssh_config()
    target_ip = ssh_cfg["host"]
    start = time.monotonic()
    try:
        code, out = run_remote_ssh_command("echo __SSH_OK__", timeout=10)
        elapsed_ms = (time.monotonic() - start) * 1000.0
        if code == 0 and "__SSH_OK__" in out:
            return True, target_ip, round(elapsed_ms, 1)
        return False, target_ip, round(elapsed_ms, 1)
    except Exception:
        elapsed_ms = (time.monotonic() - start) * 1000.0
        return False, target_ip, round(elapsed_ms, 1)
