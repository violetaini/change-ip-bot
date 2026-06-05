import base64
import datetime as dt
import hashlib
import hmac
import time
import uuid
from urllib.parse import quote

import requests

from config import config
from services.huawei_dns import HuaweiDNSClient, update_huawei_dns_if_enabled
from utils.logger import logger


HTTP_TIMEOUT = 30


def _cfg(key: str, default=""):
    value = config.get(key, default)
    if value is None:
        return default
    return value


def _text(key: str, default: str = "") -> str:
    return str(_cfg(key, default) or "").strip()


def _record_config() -> tuple[str, str, str, int]:
    zone_name = _text("dns_zone_name") or _text("huawei_dns_zone_name")
    record_name = _text("dns_record_name") or _text("huawei_dns_record_name")
    record_type = (_text("dns_record_type") or _text("huawei_dns_record_type", "A")).upper()
    ttl = int(_cfg("dns_ttl", _cfg("huawei_dns_ttl", 60)) or 60)
    return zone_name.rstrip("."), record_name.rstrip("."), record_type, ttl


def get_dns_record_name() -> str:
    return _record_config()[1]


def get_dns_provider_name() -> str:
    provider = _text("dns_provider").lower()
    if provider:
        return provider
    if config.get("huawei_dns_enabled"):
        return "huawei"
    return ""


def is_dns_update_enabled() -> bool:
    if config.get("huawei_dns_enabled"):
        return True
    return bool(config.get("dns_update_enabled") and get_dns_provider_name())


def _require_config(provider: str, values: dict[str, str]) -> None:
    missing = [key for key, value in values.items() if not str(value or "").strip()]
    if missing:
        raise RuntimeError(f"{provider} DNS配置不完整，缺少: {', '.join(missing)}")


def _relative_name(record_name: str, zone_name: str, root_value: str = "@") -> str:
    record = record_name.rstrip(".")
    zone = zone_name.rstrip(".")
    if record == zone:
        return root_value
    suffix = f".{zone}"
    if record.endswith(suffix):
        return record[: -len(suffix)] or root_value
    return record


def _request_json(method: str, url: str, **kwargs):
    response = requests.request(method, url, timeout=HTTP_TIMEOUT, **kwargs)
    try:
        payload = response.json() if response.text else {}
    except ValueError:
        payload = {}
    if not response.ok:
        detail = payload or response.text[:300]
        raise RuntimeError(f"HTTP {response.status_code}: {detail}")
    return payload


def _update_cloudflare(new_ip: str) -> str:
    zone_name, record_name, record_type, ttl = _record_config()
    token = _text("cloudflare_api_token")
    proxied = bool(config.get("cloudflare_proxied", False))
    _require_config("Cloudflare", {
        "dns_zone_name": zone_name,
        "dns_record_name": record_name,
        "cloudflare_api_token": token,
    })

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    zones = _request_json(
        "GET",
        "https://api.cloudflare.com/client/v4/zones",
        headers=headers,
        params={"name": zone_name, "status": "active"},
    )
    zone_items = zones.get("result") or []
    if not zone_items:
        raise RuntimeError(f"Cloudflare 未找到 Zone: {zone_name}")
    zone_id = zone_items[0]["id"]

    records = _request_json(
        "GET",
        f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records",
        headers=headers,
        params={"name": record_name, "type": record_type},
    )
    record_items = records.get("result") or []
    if not record_items:
        raise RuntimeError(f"Cloudflare 未找到记录: {record_name} {record_type}")
    record_id = record_items[0]["id"]

    payload = {
        "type": record_type,
        "name": record_name,
        "content": new_ip,
        "ttl": ttl,
        "proxied": proxied,
    }
    _request_json(
        "PUT",
        f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{record_id}",
        headers=headers,
        json=payload,
    )
    return f"Cloudflare: {record_name} -> {new_ip}"


def _aliyun_percent_encode(value: str) -> str:
    return quote(str(value), safe="~")


def _aliyun_request(params: dict[str, str]):
    access_key = _text("aliyun_access_key_id")
    access_secret = _text("aliyun_access_key_secret")
    _require_config("阿里云", {
        "aliyun_access_key_id": access_key,
        "aliyun_access_key_secret": access_secret,
    })

    common = {
        "Format": "JSON",
        "Version": "2015-01-09",
        "AccessKeyId": access_key,
        "SignatureMethod": "HMAC-SHA1",
        "Timestamp": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "SignatureVersion": "1.0",
        "SignatureNonce": str(uuid.uuid4()),
    }
    signed_params = {**common, **params}
    canonical = "&".join(
        f"{_aliyun_percent_encode(key)}={_aliyun_percent_encode(signed_params[key])}"
        for key in sorted(signed_params)
    )
    string_to_sign = "GET&%2F&" + _aliyun_percent_encode(canonical)
    digest = hmac.new(
        f"{access_secret}&".encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    signed_params["Signature"] = base64.b64encode(digest).decode("utf-8")
    return _request_json("GET", "https://alidns.aliyuncs.com/", params=signed_params)


def _update_aliyun(new_ip: str) -> str:
    zone_name, record_name, record_type, ttl = _record_config()
    _require_config("阿里云", {
        "dns_zone_name": zone_name,
        "dns_record_name": record_name,
    })
    rr = _relative_name(record_name, zone_name)

    records = _aliyun_request({
        "Action": "DescribeSubDomainRecords",
        "SubDomain": record_name,
        "Type": record_type,
    })
    record_items = ((records.get("DomainRecords") or {}).get("Record")) or []
    if not record_items:
        raise RuntimeError(f"阿里云未找到记录: {record_name} {record_type}")

    record_id = record_items[0]["RecordId"]
    _aliyun_request({
        "Action": "UpdateDomainRecord",
        "RecordId": record_id,
        "RR": rr,
        "Type": record_type,
        "Value": new_ip,
        "TTL": str(ttl),
    })
    return f"阿里云DNS: {record_name} -> {new_ip}"


def _update_dnspod(new_ip: str) -> str:
    zone_name, record_name, record_type, ttl = _record_config()
    login_token = _text("dnspod_login_token")
    _require_config("DNSPod", {
        "dns_zone_name": zone_name,
        "dns_record_name": record_name,
        "dnspod_login_token": login_token,
    })
    sub_domain = _relative_name(record_name, zone_name)
    base_data = {"login_token": login_token, "format": "json", "domain": zone_name}

    record_list = _request_json(
        "POST",
        "https://dnsapi.cn/Record.List",
        data={**base_data, "sub_domain": sub_domain, "record_type": record_type},
    )
    if str((record_list.get("status") or {}).get("code")) != "1":
        raise RuntimeError(f"DNSPod 查询失败: {(record_list.get('status') or {}).get('message')}")
    records = record_list.get("records") or []
    if not records:
        raise RuntimeError(f"DNSPod 未找到记录: {record_name} {record_type}")
    record = records[0]

    result = _request_json(
        "POST",
        "https://dnsapi.cn/Record.Modify",
        data={
            **base_data,
            "record_id": record["id"],
            "sub_domain": sub_domain,
            "record_type": record_type,
            "record_line_id": record.get("line_id", "0"),
            "value": new_ip,
            "ttl": str(ttl),
        },
    )
    if str((result.get("status") or {}).get("code")) != "1":
        raise RuntimeError(f"DNSPod 更新失败: {(result.get('status') or {}).get('message')}")
    return f"DNSPod: {record_name} -> {new_ip}"


def _update_godaddy(new_ip: str) -> str:
    zone_name, record_name, record_type, ttl = _record_config()
    api_key = _text("godaddy_api_key")
    api_secret = _text("godaddy_api_secret")
    _require_config("GoDaddy", {
        "dns_zone_name": zone_name,
        "dns_record_name": record_name,
        "godaddy_api_key": api_key,
        "godaddy_api_secret": api_secret,
    })
    rr = _relative_name(record_name, zone_name)
    headers = {
        "Authorization": f"sso-key {api_key}:{api_secret}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    url = f"https://api.godaddy.com/v1/domains/{zone_name}/records/{record_type}/{rr}"
    _request_json("PATCH", url, headers=headers, json=[{"data": new_ip, "ttl": ttl}])
    return f"GoDaddy: {record_name} -> {new_ip}"


def _update_porkbun(new_ip: str) -> str:
    zone_name, record_name, record_type, ttl = _record_config()
    api_key = _text("porkbun_api_key")
    secret_key = _text("porkbun_secret_api_key")
    _require_config("Porkbun", {
        "dns_zone_name": zone_name,
        "dns_record_name": record_name,
        "porkbun_api_key": api_key,
        "porkbun_secret_api_key": secret_key,
    })
    rr = _relative_name(record_name, zone_name, root_value="")
    auth = {"apikey": api_key, "secretapikey": secret_key}
    records = _request_json(
        "POST",
        f"https://api.porkbun.com/api/json/v3/dns/retrieve/{zone_name}",
        json=auth,
    )
    if records.get("status") != "SUCCESS":
        raise RuntimeError(f"Porkbun 查询失败: {records.get('message')}")
    record_items = records.get("records") or []
    matched = [
        item for item in record_items
        if str(item.get("type", "")).upper() == record_type
        and str(item.get("name", "")).rstrip(".") == record_name
    ]
    if not matched:
        raise RuntimeError(f"Porkbun 未找到记录: {record_name} {record_type}")

    result = _request_json(
        "POST",
        f"https://api.porkbun.com/api/json/v3/dns/edit/{zone_name}/{matched[0]['id']}",
        json={
            **auth,
            "type": record_type,
            "name": rr,
            "content": new_ip,
            "ttl": str(ttl),
        },
    )
    if result.get("status") != "SUCCESS":
        raise RuntimeError(f"Porkbun 更新失败: {result.get('message')}")
    return f"Porkbun: {record_name} -> {new_ip}"


def _update_digitalocean(new_ip: str) -> str:
    zone_name, record_name, record_type, ttl = _record_config()
    token = _text("digitalocean_token")
    _require_config("DigitalOcean", {
        "dns_zone_name": zone_name,
        "dns_record_name": record_name,
        "digitalocean_token": token,
    })
    rr = _relative_name(record_name, zone_name)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    records = _request_json(
        "GET",
        f"https://api.digitalocean.com/v2/domains/{zone_name}/records",
        headers=headers,
    )
    record_items = [
        item for item in (records.get("domain_records") or [])
        if str(item.get("type", "")).upper() == record_type
        and str(item.get("name", "")).rstrip(".") in {rr, record_name}
    ]
    if not record_items:
        raise RuntimeError(f"DigitalOcean 未找到记录: {record_name} {record_type}")
    record_id = record_items[0]["id"]
    _request_json(
        "PATCH",
        f"https://api.digitalocean.com/v2/domains/{zone_name}/records/{record_id}",
        headers=headers,
        json={"type": record_type, "name": rr, "data": new_ip, "ttl": ttl},
    )
    return f"DigitalOcean: {record_name} -> {new_ip}"


PROVIDERS = {
    "cloudflare": _update_cloudflare,
    "aliyun": _update_aliyun,
    "alicloud": _update_aliyun,
    "dnspod": _update_dnspod,
    "tencent_dnspod": _update_dnspod,
    "godaddy": _update_godaddy,
    "porkbun": _update_porkbun,
    "digitalocean": _update_digitalocean,
}


def update_dns_if_enabled(new_ip: str) -> str:
    if config.get("huawei_dns_enabled") and not _text("dns_provider"):
        return update_huawei_dns_if_enabled(new_ip)

    if not config.get("dns_update_enabled"):
        return "未启用DNS更新"

    provider = get_dns_provider_name()
    if provider == "huawei":
        return HuaweiDNSClient().update_record(new_ip)

    update_func = PROVIDERS.get(provider)
    if not update_func:
        raise RuntimeError(f"不支持的DNS服务商: {provider or '未配置'}")

    start = time.monotonic()
    result = update_func(new_ip)
    logger.info(f"DNS更新成功: provider={provider}, elapsed={time.monotonic() - start:.1f}s")
    return result
