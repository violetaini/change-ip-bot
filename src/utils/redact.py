import re


SENSITIVE_PATTERNS = [
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"(?i)(token|api[_-]?key|secret|password|passwd|authorization)=([^&\s]+)"),
    re.compile(r"(?i)(token|api[_-]?key|secret|password|passwd|authorization):\s*([^\s,]+)"),
]


def redact_text(text: str) -> str:
    redacted = str(text or "")

    try:
        from config import config

        for key in (
            "telegram_bot_token",
            "ip_change_api",
            "huawei_ak",
            "huawei_sk",
            "cloudflare_api_token",
            "aliyun_access_key_id",
            "aliyun_access_key_secret",
            "dnspod_login_token",
            "godaddy_api_key",
            "godaddy_api_secret",
            "porkbun_api_key",
            "porkbun_secret_api_key",
            "digitalocean_token",
        ):
            value = str(config.get(key, "") or "").strip()
            if len(value) >= 6:
                redacted = redacted.replace(value, f"<redacted:{key}>")
    except Exception:
        pass

    for pattern in SENSITIVE_PATTERNS:
        def replace_match(match):
            if match.lastindex and match.lastindex >= 2:
                return match.group(0).split(match.group(2))[0] + "<redacted>"
            return "<redacted>"

        redacted = pattern.sub(replace_match, redacted)

    return redacted
