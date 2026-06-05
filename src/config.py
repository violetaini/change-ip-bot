import os
import yaml
from typing import Dict, Any

DEFAULT_CONFIG = {
    "telegram_allowed_user_ids": "",
    "telegram_super_admin_user_ids": "",
    "telegram_admin_user_ids": "",
    "ip_check_cmd": "curl -s api-ipv4.ip.sb/ip",
    "ip_check_api": "",
    "ip_check_timeout": 60,
    "state_file": "/var/lib/vps-ip-bot/state.json",
    "ip_change_api": "",
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
    "ping_count": 10,
    "ip_quality_enabled": True,
    "ip_quality_cmd": "bash <(curl -sL https://IP.Check.Place) -y",
    "stream_check_enabled": True,
    "stream_check_cmd": "bash <(curl -L -s https://github.com/1-stream/RegionRestrictionCheck/raw/main/check.sh)",
    "stream_check_input": "1",
    "stream_check_timeout": 1200,
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


config = load_config()
