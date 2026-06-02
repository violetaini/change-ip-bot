# VPS IP Bot

A Telegram bot for VPS servers that already have an IP switch API.

This project is useful when your server provider, panel, or custom script exposes an HTTP API that can change the server's public IP. The bot wraps that API with Telegram commands, checks whether the IP really changed, and can update Huawei Cloud DNS after a successful change.

## What It Does

- Check the current public IP.
- Trigger your existing IP change API from Telegram.
- Verify the new public IP after the API returns.
- Treat API timeouts as success if the public IP actually changed.
- Update a Huawei Cloud DNS record when enabled.
- Send the result back to Telegram, with retry notification support.
- Run scheduled automatic IP changes if enabled.
- Run simple network tools such as ping, speedtest, and IP quality reports.

## Important Assumption

This bot does not create an IP change service by itself.

You must already have an API like this:

```text
https://example.com/change-ip
```

The API should return JSON similar to:

```json
{
  "status": "IP changed",
  "old_ip": "1.2.3.4",
  "new_ip": "5.6.7.8"
}
```

The bot also supports:

```json
{
  "status": "IP unchanged",
  "old_ip": "1.2.3.4",
  "new_ip": "1.2.3.4"
}
```

## Telegram Commands

```text
/start      Show help
/check      Check current IP status
/change     Change IP and optionally update Huawei Cloud DNS
/quality    Run IP quality check and send an image report
/ping       Test network latency
/speedtest  Run network speed test
```

## Installation

Clone or upload the project to your server, for example:

```bash
mkdir -p /opt/vps-change-ip
cd /opt/vps-change-ip
```

Create a virtual environment and install dependencies:

```bash
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
ip_change_api: ""
```

Optional but commonly used:

```yaml
telegram_allowed_user_ids: ""
auto_change_enabled: false
auto_change_interval_minutes: 360
huawei_dns_enabled: false
huawei_ak: ""
huawei_sk: ""
huawei_dns_zone_name: ""
huawei_dns_record_name: ""
```

`telegram_chat_id` can contain one or more chat IDs separated by commas.

If `telegram_allowed_user_ids` is empty, the bot keeps the old behavior and authorizes by `telegram_chat_id`. If you run the bot in a group and want to restrict who can use it, set `telegram_allowed_user_ids`.

Do not commit `config.yaml`. It may contain secrets.

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
- `/speedtest` requires the `speedtest` CLI to be installed on the server.

## License

This project is released under the MIT License. See [LICENSE](LICENSE) for details.
