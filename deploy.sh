#!/usr/bin/env bash
set -e
APP_DIR="/opt/vps-change-ip"
SERVICE_FILE="/etc/systemd/system/vps-ip-bot.service"
mkdir -p "$APP_DIR"
unzip -o "${1:-vps-ip-bot-share.zip}" -d "$APP_DIR"
cd "$APP_DIR"
python3 -m venv venv
source venv/bin/activate
python -m pip install -U pip
pip install -r requirements.txt
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=VPS IP Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python $APP_DIR/src/bot.py
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable vps-ip-bot
printf '部署完成。请编辑 %s/config.yaml 后启动服务：\n' "$APP_DIR"
printf '  systemctl start vps-ip-bot\n  systemctl status vps-ip-bot --no-pager\n'
