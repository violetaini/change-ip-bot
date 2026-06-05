VPS IP Bot 简洁部署说明

一、服务器准备
1. 安装基础环境：
   apt update
   apt install -y python3 python3-pip python3-venv unzip \
      curl fonts-noto-cjk fonts-wqy-zenhei fonts-wqy-microhei
   如需优先用浏览器截图 /quality 报告，可额外安装 chromium；
   未安装 chromium 时会使用 Python 依赖中的 CairoSVG 渲染。

二、上传并解压
1. 把压缩包上传到服务器，例如 /root/vps-ip-bot-share.zip
2. 执行：
   mkdir -p /opt/vps-change-ip
   unzip -o /root/vps-ip-bot-share.zip -d /opt/vps-change-ip
   cd /opt/vps-change-ip

三、配置
1. 编辑配置文件：
   nano /opt/vps-change-ip/config.yaml
2. 至少填写这些：
   telegram_bot_token
   telegram_chat_id
   ip_change_api
   如果机器人在群里使用，telegram_chat_id 可填群 chat_id；
   如需限制具体操作者，再填写 telegram_super_admin_user_ids 和 telegram_admin_user_ids（多个用英文逗号分隔）。
3. 如果要自动更新 DNS，可使用统一 DNS 配置：
   dns_update_enabled: true
   dns_provider: cloudflare
   dns_zone_name
   dns_record_name
   dns_record_type: A
   dns_ttl: 60
   然后按服务商填写对应密钥。
   当前支持：huawei、cloudflare、aliyun、dnspod、tencent_dnspod、godaddy、porkbun、digitalocean。
   旧的华为云配置仍兼容：
   huawei_dns_enabled: true
   huawei_ak
   huawei_sk
   huawei_dns_zone_name
   huawei_dns_record_name

四、安装依赖
执行：
   cd /opt/vps-change-ip
   python3 -m venv venv
   source /opt/vps-change-ip/venv/bin/activate
   python -m pip install -U pip
   pip install -r requirements.txt

五、前台测试
执行：
   cd /opt/vps-change-ip
   source /opt/vps-change-ip/venv/bin/activate
   python src/bot.py

先在 Telegram 测试：
   /start
   /check
   /change
   /auto_start
   /auto_stop
   /auto_status
   /set_auto_time 04:00
   /add_admin 123456789
   /logs
   /health
   /quality
   /stream

六、注册 systemd 服务
执行：
cat > /etc/systemd/system/vps-ip-bot.service <<'EOF'
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
EOF

systemctl daemon-reload
systemctl enable vps-ip-bot
systemctl start vps-ip-bot
systemctl status vps-ip-bot --no-pager

七、常用命令
重启：systemctl restart vps-ip-bot
停止：systemctl stop vps-ip-bot
状态：systemctl status vps-ip-bot --no-pager
日志：journalctl -u vps-ip-bot -f

八、说明
1. /change 支持按 JSON 返回 status/new_ip/old_ip 判断结果。
2. 接口超时但公网 IP 已变化时，会按成功处理。
3. /quality 会发送 JPG 图片预览。
4. 自动换 IP 默认关闭，可用 /auto_start 开启，/auto_stop 关闭，/auto_status 查看状态。
5. 状态文件默认写入 /var/lib/vps-ip-bot/state.json，可通过 state_file 配置或 VPS_IP_BOT_STATE_FILE 环境变量覆盖。
6. /quality 默认执行 IP.Check.Place 远程脚本，如不需要可设置 ip_quality_enabled: false，或用 ip_quality_cmd 指定自己的检测命令。
7. /stream 默认执行 1-stream/RegionRestrictionCheck 脚本，自动输入 1，并把结果整理成简报发送。
8. 自动换 IP 默认按北京时间 04:00 执行，可用 /set_auto_time HH:MM 修改；失败后默认最多重试 5 次。
9. 普通管理员可执行 /change 和只读检测命令；/auto_start、/auto_stop、/set_auto_time、/logs、/add_admin 仅超级管理员可用。
10. /add_admin USER_ID 可由超级管理员添加普通管理员，并写回 config.yaml。
11. 自动换 IP 成功后会先更新配置的 DNS 服务商，再发送换 IP 结果、等待并检查 DNS 是否解析到新 IP，最后发送 IP 质量图片。
12. 日志写入和 /logs 输出都会对 bot token、换 IP API、华为云 AK/SK 等敏感信息做脱敏。
