import os
import yaml
from typing import Any, Dict, Optional

DEFAULT_CONFIG = {
    "telegram_allowed_user_ids": "",
    "telegram_super_admin_user_ids": "",
    "telegram_admin_user_ids": "",
    "ip_check_cmd": "curl -s api-ipv4.ip.sb/ip",
    "ip_check_api": "",
    "ip_check_timeout": 60,
    "state_file": "/var/lib/vps-ip-bot/state.json",
    "ip_change_provider": "generic",
    "ip_change_api": "",
    "ip_change_poll_retries": 18,
    "ip_change_poll_delay": 5,
    "boil_api_base_url": "https://ippanel.boil.network",
    "boil_api_token": "",
    "ip_change_interval": 2,
    "ip_change_timeout": 600,
    "ip_change_verify_public_ip": True,
    "ip_change_verify_delay": 5,
    "ip_change_retry_verify_count": 3,
    "auto_change_enabled": False,
    "auto_change_interval_minutes": 360,
    "auto_change_time": "04:00",
    "auto_change_retry_count": 5,
    "auto_change_retry_delay_seconds": 60,
    "auto_change_notify": True,
    "auto_change_quality_report": True,
    "dns_verify_enabled": True,
    "dns_verify_delay_seconds": 60,
    "dns_verify_retry_count": 10,
    "dns_update_enabled": False,
    "dns_provider": "",
    "dns_zone_name": "",
    "dns_record_name": "",
    "dns_record_type": "A",
    "dns_ttl": 60,
    "cloudflare_api_token": "",
    "cloudflare_proxied": False,
    "aliyun_access_key_id": "",
    "aliyun_access_key_secret": "",
    "dnspod_login_token": "",
    "godaddy_api_key": "",
    "godaddy_api_secret": "",
    "porkbun_api_key": "",
    "porkbun_secret_api_key": "",
    "digitalocean_token": "",
    "huawei_dns_enabled": False,
    "huawei_ak": "",
    "huawei_sk": "",
    "huawei_dns_zone_name": "",
    "huawei_dns_record_name": "",
    "huawei_dns_record_type": "A",
    "huawei_dns_ttl": 60,
    "ping_target": "1.1.1.1",
    "ping_target_v6": "2606:4700:4700::1111",
    "ping_count": 10,
    "ip_quality_enabled": True,
    "ip_quality_cmd": "bash <(curl -sL https://IP.Check.Place) -y",
    "stream_check_enabled": True,
    "stream_check_cmd": "bash <(curl -L -s https://github.com/1-stream/RegionRestrictionCheck/raw/main/check.sh)",
    "stream_check_input": "2",
    "stream_check_timeout": 1200,
    "remote_ssh_enabled": False,
    "remote_ssh_host": "",
    "remote_ssh_port": 22,
    "remote_ssh_user": "root",
    "remote_ssh_key_path": "",
    "remote_ssh_password": "",
    "remote_ssh_timeout": 300,
}


def _to_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def load_config() -> Dict[str, Any]:
    config_paths = [
        "config.yaml",
        os.path.join(os.path.dirname(__file__), "..", "config.yaml"),
        "/etc/vps-ip-bot/config.yaml",
    ]

    user_config = None
    used_path = None

    for path in config_paths:
        real_path = os.path.abspath(path)
        if os.path.exists(real_path):
            with open(real_path, "r", encoding="utf-8") as f:
                user_config = yaml.safe_load(f) or {}
            used_path = real_path
            break

    if user_config is None:
        raise FileNotFoundError(
            f"未找到配置文件，请确认以下路径之一存在: {config_paths}"
        )

    config = {**DEFAULT_CONFIG, **user_config}

    for key, val in list(config.items()):
        if key.endswith("_user_ids") and val is None:
            config[key] = ""

    valid_providers = ("generic", "fachost", "boil", "classic")
    raw_provider = str(config.get("ip_change_provider") or "generic").strip().lower()
    if raw_provider == "classic":
        config["ip_change_provider"] = "fachost"
    elif raw_provider in valid_providers:
        config["ip_change_provider"] = raw_provider
    else:
        config["ip_change_provider"] = "generic"

    required_fields = ["telegram_bot_token", "telegram_chat_id"]
    for field in required_fields:
        if not config.get(field):
            raise ValueError(f"配置文件缺少必要字段: {field}")

    config["ip_change_verify_public_ip"] = _to_bool(config.get("ip_change_verify_public_ip"))
    config["auto_change_enabled"] = _to_bool(config.get("auto_change_enabled"))
    config["auto_change_notify"] = _to_bool(config.get("auto_change_notify"))
    config["dns_update_enabled"] = _to_bool(config.get("dns_update_enabled"))
    config["cloudflare_proxied"] = _to_bool(config.get("cloudflare_proxied"))
    config["huawei_dns_enabled"] = _to_bool(config.get("huawei_dns_enabled"))
    config["ip_quality_enabled"] = _to_bool(config.get("ip_quality_enabled"))
    config["stream_check_enabled"] = _to_bool(config.get("stream_check_enabled"))
    config["_loaded_from"] = used_path
    return config


def normalize_server_config(server_raw: Dict[str, Any], global_cfg: Dict[str, Any], default_id: str = "default") -> Dict[str, Any]:
    merged = {**global_cfg, **server_raw}
    s_id = str(server_raw.get("id") or default_id).strip()
    s_name = str(server_raw.get("name") or s_id).strip()
    merged["id"] = s_id
    merged["name"] = s_name

    valid_providers = ("generic", "fachost", "boil", "classic")
    raw_provider = str(merged.get("ip_change_provider") or "generic").strip().lower()
    if raw_provider == "classic":
        merged["ip_change_provider"] = "fachost"
    elif raw_provider in valid_providers:
        merged["ip_change_provider"] = raw_provider
    else:
        merged["ip_change_provider"] = "generic"

    merged.pop("servers", None)
    merged["remote_ssh_enabled"] = _to_bool(merged.get("remote_ssh_enabled"))
    merged["remote_ssh_port"] = int(merged.get("remote_ssh_port") or 22)
    merged["ip_change_interval"] = int(merged.get("ip_change_interval") or 2)
    merged["ip_change_timeout"] = int(merged.get("ip_change_timeout") or 600)
    merged["ip_change_poll_retries"] = int(merged.get("ip_change_poll_retries") or 18)
    merged["ip_change_poll_delay"] = int(merged.get("ip_change_poll_delay") or 5)
    merged["ip_change_verify_public_ip"] = _to_bool(merged.get("ip_change_verify_public_ip"))
    merged["auto_change_enabled"] = _to_bool(merged.get("auto_change_enabled"))
    merged["auto_change_notify"] = _to_bool(merged.get("auto_change_notify"))
    merged["auto_change_quality_report"] = _to_bool(merged.get("auto_change_quality_report"))
    merged["dns_update_enabled"] = _to_bool(merged.get("dns_update_enabled"))
    merged["cloudflare_proxied"] = _to_bool(merged.get("cloudflare_proxied"))
    merged["huawei_dns_enabled"] = _to_bool(merged.get("huawei_dns_enabled"))
    merged["ip_quality_enabled"] = _to_bool(merged.get("ip_quality_enabled"))
    merged["stream_check_enabled"] = _to_bool(merged.get("stream_check_enabled"))
    return merged


def get_servers(cfg: Optional[Dict[str, Any]] = None) -> list[Dict[str, Any]]:
    target_cfg = cfg if cfg is not None else config
    raw_servers = target_cfg.get("servers")
    if isinstance(raw_servers, list) and len(raw_servers) > 0:
        res = []
        for idx, item in enumerate(raw_servers):
            if isinstance(item, dict):
                norm = normalize_server_config(item, target_cfg, default_id=f"server_{idx + 1}")
                res.append(norm)
        if res:
            return res

    # 兼容单服务器旧配置
    single_raw = {
        "id": str(target_cfg.get("default_server_id") or "default").strip(),
        "name": str(target_cfg.get("default_server_name") or "默认服务器").strip(),
    }
    return [normalize_server_config(single_raw, target_cfg, default_id="default")]


def get_server_config(server_id: Optional[str] = None, cfg: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    servers = get_servers(cfg)
    if not servers:
        return None
    if not server_id:
        return servers[0]

    clean_id = str(server_id).strip().lower()
    for s in servers:
        if s.get("id", "").strip().lower() == clean_id:
            return s
    for s in servers:
        if s.get("name", "").strip().lower() == clean_id:
            return s
    return None


def is_multi_server_mode(cfg: Optional[Dict[str, Any]] = None) -> bool:
    return len(get_servers(cfg)) > 1


try:
    config = load_config()
except FileNotFoundError:
    config = {**DEFAULT_CONFIG, "_loaded_from": None}


