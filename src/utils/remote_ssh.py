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

_DNS_LOCAL_CACHE: Dict[str, Tuple[str, float]] = {}
_CACHE_TTL = 60.0  # 本地内存DNS缓存过期时间（秒）


def set_cached_host_ip(hostname: str, ip: str) -> None:
    clean_host = str(hostname or "").strip().rstrip(".").lower()
    clean_ip = str(ip or "").strip()
    if clean_host and clean_ip:
        _DNS_LOCAL_CACHE[clean_host] = (clean_ip, time.time())
        logger.info(f"本地DNS映射更新: {clean_host} -> {clean_ip} (TTL={_CACHE_TTL}s)")


def get_cached_host_ip(hostname: str, max_age: float = _CACHE_TTL) -> str:
    clean_host = str(hostname or "").strip().rstrip(".").lower()
    entry = _DNS_LOCAL_CACHE.get(clean_host)
    if entry:
        ip, ts = entry
        if (time.time() - ts) <= max_age:
            return ip
    return ""


def invalidate_cached_host_ip(hostname: str) -> None:
    clean_host = str(hostname or "").strip().rstrip(".").lower()
    if clean_host in _DNS_LOCAL_CACHE:
        del _DNS_LOCAL_CACHE[clean_host]
        logger.info(f"本地DNS映射已主动失效: {clean_host}")


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


def resolve_target_host(hostname: str, force_refresh: bool = False) -> str:
    clean_host = str(hostname or "").strip().rstrip(".")
    if not clean_host:
        return ""

    # 1. Already an IPv4
    if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", clean_host):
        return clean_host

    # 2. Check local memory cache (if not forcing fresh probe)
    if not force_refresh:
        cached = get_cached_host_ip(clean_host)
        if cached:
            return cached

    # 3. If Boil mode, query live Boil API (fastest authoritative source)
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
    except Exception as e:
        logger.debug(f"Boil API 获取实时IP异常: {e}")

    # 4. Query Cloudflare 1.1.1.1 DoH directly
    doh_ip = resolve_via_cloudflare_doh(clean_host, timeout=5)
    if doh_ip:
        set_cached_host_ip(clean_host, doh_ip)
        return doh_ip

    # 5. Fallback: if force_refresh failed to get new IP, return expired cache if exists
    entry = _DNS_LOCAL_CACHE.get(clean_host.lower())
    if entry:
        return entry[0]

    return clean_host


def is_remote_ssh_enabled() -> bool:
    return bool(config.get("remote_ssh_enabled", False))


def get_ssh_config(force_refresh: bool = False) -> Dict[str, Any]:
    raw_host = str(config.get("remote_ssh_host") or "").strip()
    target_ip = resolve_target_host(raw_host, force_refresh=force_refresh)

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
    allow_retry_on_ip_change: bool = True,
) -> Tuple[int, str]:
    ssh_cfg = get_ssh_config(force_refresh=False)
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
        # 针对 SSH 常见连接失败返回码（255：网络不可达/拒绝连接/主机失联），尝试自愈重试
        if res.returncode == 255 and allow_retry_on_ip_change and ssh_cfg.get("raw_host"):
            logger.warning(f"远程 SSH 连接失败 (code 255)，检查目标 IP 是否已变更...")
            new_ip = resolve_target_host(ssh_cfg["raw_host"], force_refresh=True)
            if new_ip and new_ip != ssh_cfg["host"]:
                logger.info(f"检测到目标 IP 已变更 ({ssh_cfg['host']} -> {new_ip})，正在自动重试 SSH 执行...")
                return run_remote_ssh_command(command_str, timeout, input_data, allow_retry_on_ip_change=False)

        output = (res.stdout or "") + ("\n" + res.stderr if res.stderr else "")
        return res.returncode, output.strip()
    except subprocess.TimeoutExpired as e:
        if allow_retry_on_ip_change and ssh_cfg.get("raw_host"):
            logger.warning(f"远程 SSH 执行超时，检查目标 IP 是否已变更...")
            new_ip = resolve_target_host(ssh_cfg["raw_host"], force_refresh=True)
            if new_ip and new_ip != ssh_cfg["host"]:
                logger.info(f"检测到目标 IP 已变更 ({ssh_cfg['host']} -> {new_ip})，正在自动重试 SSH 执行...")
                return run_remote_ssh_command(command_str, timeout, input_data, allow_retry_on_ip_change=False)

        msg = f"远程 SSH 执行超时（{timeout}秒，目标: {ssh_cfg['host']}:{ssh_cfg['port']}）"
        logger.warning(msg)
        raise TimeoutError(msg) from e
    except Exception as e:
        msg = f"远程 SSH 执行异常: {redact_text(str(e))}"
        logger.error(msg)
        raise RuntimeError(msg) from e


def test_remote_ssh_connectivity(force_refresh: bool = False) -> Tuple[bool, str, float]:
    """Test SSH connectivity and return (success, resolved_ip, rtt_ms)."""
    ssh_cfg = get_ssh_config(force_refresh=force_refresh)
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
