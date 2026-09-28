# VPS IP Bot

A Telegram bot for VPS and proxy servers with automated IP switching, multi-provider DNS updating, scheduled changes, and network diagnostic tools.

Supports both **Classic HTTP IP Change APIs** and **Boil Network residential IP API** with dynamic in-bot mode switching and client-side quota protection.

## What It Does

- **Dual-mode IP switching**:
  - **Classic Mode**: Integrates with custom VPS panel / provider HTTP APIs.
  - **Boil Network Mode**: Direct integration with Boil Network residential IP API (`changeIP` & `getIP`).
- **Cooldown & Quota Protection**: Respects Boil Network's `next_allowed_at` timestamp client-side to prevent penalizing daily quotas.
- **Telegram Bot Control**:
  - Query IP and change status (`/check`, `/ip_status`).
  - Trigger manual IP change (`/change`).
  - Switch provider modes dynamically via buttons or commands (`/set_ip_mode`).
  - Set API URLs and tokens directly from chat (`/set_ip_api`, `/set_boil_token`).
  - Clears legacy WebApp / Mini App menu buttons automatically on startup.
- **Multi-Provider DNS Automation**:
  - Automatically updates DNS A records upon IP change.
  - Supports **Cloudflare**, **Aliyun**, **Tencent / DNSPod**, **GoDaddy**, **Porkbun**, **DigitalOcean**, and **Huawei Cloud**.
  - Verifies public DNS propagation after scheduled changes.
- **Scheduled Automatic Changes**:
  - Run automatic IP changes at fixed Beijing time (`/set_auto_time`, `/auto_start`, `/auto_stop`).
  - Configurable retries, delays, and notifications.
- **Diagnostics & Network Tools**:
  - IP quality reports with image generation (`/quality`).
  - Streaming service unlock checking (`/stream`).
  - Latency ping (`/ping`) and network speed test (`/speedtest`).
- **Security & Privacy**:
  - Strict role-based access control (Super Admin vs Admin).
  - Sensitive token / secret redaction across all logs and messages.

## Supported IP Change Providers

### 1. Classic Mode (`classic`)
- Generic HTTP API returning JSON with `status`, `old_ip`, and `new_ip`.
- Cooldown controlled locally by `ip_change_interval` (minutes).
- Verifies public IP change after API call.

### 2. Boil Network Mode (`boil`)
- Official API integration for Boil Network (`https://ippanel.boil.network`).
- Queries current residential IP via `POST /api/v1/getIP` without consuming change quotas.
- Triggers IP change via `POST /api/v1/changeIP`.
- Displays remaining daily quota (`uses_left`) and dynamic server cooldown (`next_allowed_at`).
- Client-side cooldown guard (`COOLDOWN_PROTECTION`) prevents accidental early requests that could consume penalty quotas.
- Polls for new IP via API and updates configured DNS records automatically.

## Telegram Commands

```text
/start               Show help message
/check               Check current public IP (or Boil residential IP)
/change              Trigger IP change and update DNS
/ip_status           Show current IP change mode, cooldown status, and quota
/set_ip_mode [mode]  Switch IP mode (classic/boil) with interactive buttons, super admin only
/set_boil_token      Set Boil API Token, super admin only
/set_ip_api [url]    Set Classic IP change API URL, super admin only
/auto_start          Enable scheduled automatic IP changes, super admin only
/auto_stop           Disable scheduled automatic IP changes, super admin only
/auto_status         Show automatic IP change status
/set_auto_time HH:MM Set daily automatic IP change time (Beijing time), super admin only
/manage_users        Manage regular admins with interactive buttons, super admin only
/logs [N]            Show recent bot logs (redacted), super admin only
/health              Run a bot health check
/dns_status          Show DNS update configuration, super admin only
/set_dns_provider    Set DNS provider, super admin only
/set_dns_record      Set DNS zone and record, super admin only
/dns_update_on       Enable DNS updates, super admin only
/dns_update_off      Disable DNS updates, super admin only
/quality             Run IP quality check and send an image report
/stream              Run streaming unlock check and send summary
/ping                Test network latency
/speedtest           Run network speed test
```

## Installation

Clone or upload the project to your server, for example:

```bash
mkdir -p /opt/vps-change-ip
cd /opt/vps-change-ip
```

Create a virtual environment and install dependencies:

```bash
apt install -y curl
python3 -m venv venv
source venv/bin/activate
python -m pip install -U pip
pip install -r requirements.txt
```

## Configuration

Copy the example config:

```bash
cp config.yaml.example config.yaml
```

Edit it:

```bash
nano config.yaml
```

Required fields:

```yaml
telegram_bot_token: ""
telegram_chat_id: ""
```

### IP Change Mode Configuration

Choose one of the following modes:

**Option A: Boil Network Residential IP Mode**
```yaml
ip_change_provider: "boil"
boil_api_base_url: "https://ippanel.boil.network"
boil_api_token: "your_boil_api_token"
```

**Option B: Classic Custom HTTP API Mode**
```yaml
ip_change_provider: "classic"
ip_change_api: "https://your-panel.com/api/change-ip"
ip_change_interval: 2
ip_change_timeout: 600
```

### Optional & Advanced Settings

```yaml
telegram_allowed_user_ids: ""
telegram_super_admin_user_ids: ""
telegram_admin_user_ids: ""

# Automatic Scheduled Changes
auto_change_enabled: false
auto_change_time: "04:00"
auto_change_retry_count: 5
auto_change_retry_delay_seconds: 60
auto_change_quality_report: true

# Public DNS Propagation Verification
dns_verify_enabled: true
dns_verify_delay_seconds: 60
dns_verify_retry_count: 10

# Multi-Provider DNS Settings (e.g. Cloudflare)
dns_update_enabled: false
dns_provider: "cloudflare"
dns_zone_name: "example.com"
dns_record_name: "sub.example.com"
dns_record_type: "A"
dns_ttl: 60
cloudflare_api_token: "your_cloudflare_api_token"
cloudflare_proxied: false

# Legacy Huawei Cloud DNS (Supported)
huawei_dns_enabled: false
huawei_ak: ""
huawei_sk: ""
huawei_dns_zone_name: ""
huawei_dns_record_name: ""

# Diagnostic Scripts
stream_check_enabled: true
stream_check_input: "1"
stream_check_timeout: 1200
```

`telegram_chat_id` can contain one or more chat IDs separated by commas.

Super admins can run sensitive commands such as `/auto_start`, `/auto_stop`, `/set_auto_time`, `/logs`, `/set_ip_mode`, `/set_boil_token`, `/set_ip_api`, and `/manage_users`.
Regular admins can run `/change` and read-only check commands.

If `telegram_super_admin_user_ids` and `telegram_admin_user_ids` are both empty, the bot keeps the legacy behavior and authorizes by `telegram_allowed_user_ids` or `telegram_chat_id`.

Do not commit `config.yaml`. It contains secrets.

Supported DNS providers:

```text
cloudflare
aliyun
dnspod
tencent_dnspod
godaddy
porkbun
digitalocean
huawei
```

## Running Tests

An automated unit test suite is included in `tests/test_all.py`, testing configuration loading, text redaction, state management, provider routing, cooldown protection, and error handling:

```bash
python -m unittest tests/test_all.py
```

## Run Manually

```bash
cd /opt/vps-change-ip
source venv/bin/activate
python src/bot.py
```

## Run With systemd

Create `/etc/systemd/system/vps-ip-bot.service`:

```ini
[Unit]
Description=VPS IP Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/vps-change-ip
ExecStart=/opt/vps-change-ip/venv/bin/python /opt/vps-change-ip/src/bot.py
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
```

Enable and start it:

```bash
systemctl daemon-reload
systemctl enable vps-ip-bot
systemctl start vps-ip-bot
systemctl status vps-ip-bot --no-pager
```

View logs:

```bash
journalctl -u vps-ip-bot -f
```

## Notes

- `config.yaml` is ignored by Git on purpose.
- The bot stores runtime state in `/var/lib/vps-ip-bot/state.json` by default.
- You can override the state file path with `state_file` or the `VPS_IP_BOT_STATE_FILE` environment variable.
- `/quality` can use Chromium if installed. If Chromium is not available, it falls back to CairoSVG.
- `/stream` runs the RegionRestrictionCheck script, automatically inputs `1`, and sends a concise summary instead of the full raw output.
- `/manage_users` can only be used by a super admin and provides button-based regular admin management. Admins are shown as buttons; tap one to select it, then tap delete. Adding an admin uses the button flow and then asks for the Telegram user ID.
- `/set_dns_provider`, `/set_dns_record`, `/dns_update_on`, and `/dns_update_off` can only be used by a super admin and write non-secret DNS settings to `config.yaml`.
- `/speedtest` requires the `speedtest` CLI to be installed on the server.
- Automatic IP changes update DNS through the configured provider, send the change result, verify DNS propagation, then send the IP quality image report.
- Logs are redacted before writing and before being sent through `/logs`, but do not commit `config.yaml` or any backup containing secrets.

## License

This project is released under the MIT License. See [LICENSE](LICENSE) for details.
