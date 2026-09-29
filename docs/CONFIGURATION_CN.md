# VPS IP Bot 详细配置指南 (Configuration Guide)

本文档提供 `config.yaml` 全部配置项的深度解析、最佳实践与多场景示例。

---

## 目录

1. [配置文件加载与覆盖机制](#1-配置文件加载与覆盖机制)
2. [Telegram 机器人与角色鉴权配置](#2-telegram-机器人与角色鉴权配置)
3. [换 IP 模式与核心接口配置](#3-换-ip-模式与核心接口配置)
   - [A. Boil Network 住宅家宽模式](#a-boil-network-住宅家宽模式)
   - [B. 经典自建 HTTP API 模式](#b-经典自建-http-api-模式)
4. [多云厂商 DNS 自动更新配置](#4-多云厂商-dns-自动更新配置)
5. [每日自动换 IP 与递归生效校验](#5-每日自动换-ip-与递归生效校验)
6. [远程家宽 SSH 诊断通道与自愈配置](#6-远程家宽-ssh-诊断通道与自愈配置)
7. [网络诊断命令微调 (Ping / Quality / Stream)](#7-网络诊断命令微调-ping--quality--stream)
8. [完整样例配置模板](#8-完整样例配置模板)

---

## 1. 配置文件加载与覆盖机制

- **文件定位**：程序启动时默认读取与项目同级的 `config.yaml`。若文件不存在，则从内置的默认配置字典加载基线参数。
- **状态存储文件 (`state_file`)**：
  - 用于持久化记录冷却时间戳、每日剩余配额、通知状态及待办任务。
  - 默认路径：`/var/lib/vps-ip-bot/state.json`。
  - **环境变量覆盖**：可通过环境变量 `VPS_IP_BOT_STATE_FILE=/path/to/state.json` 覆盖路径（容器或多实例环境极其有用）。
- **安全规范**：`config.yaml` 包含 API 密钥、Bot Token 与 SSH 凭证，`.gitignore` 已默认将其排除，**严禁提交至公共 Git 仓库**。

---

## 2. Telegram 机器人与角色鉴权配置

```yaml
telegram_bot_token: "123456789:ABCdefGhIJKlmNoPQRstuvWXyz"
telegram_chat_id: "-1001234567890,987654321"
telegram_allowed_user_ids: ""
telegram_super_admin_user_ids: "987654321"
telegram_admin_user_ids: "11223344,55667788"
```

| 参数项 | 类型 | 必填 | 默认值 | 说明与权限 |
| :--- | :---: | :---: | :---: | :--- |
| `telegram_bot_token` | string | **是** | 空 | 从 [@BotFather](https://t.me/BotFather) 获取的机器人 API Token。 |
| `telegram_chat_id` | string | **是** | 空 | 允许交互或接收通知的聊天 ID。支持多个（用英文逗号 `,` 分隔）。可为群组 ID（负数）或个人 ID。 |
| `telegram_allowed_user_ids`| string | 否 | 空 | （旧版兼容项）允许操作机器人的白名单用户 ID 列表。 |
| `telegram_super_admin_user_ids`| string | 建议 | 空 | **超级管理员用户 ID**。拥有完整特权，包括定时任务、修改模式与密钥、管理管理员及日志查看。 |
| `telegram_admin_user_ids` | string | 否 | 空 | **普通管理员用户 ID**。允许手动执行 `/change`、`/check` 及其他只读网络诊断命令。 |

> [!TIP]
> **权限等级说明**：
> - 若 `telegram_super_admin_user_ids` 和 `telegram_admin_user_ids` 均为空，系统将自动回退为旧版鉴权逻辑（凡是在 `telegram_chat_id` 或 `telegram_allowed_user_ids` 中的用户均可执行常规操作）。
> - 超级管理员可在 Telegram 中直接使用 `/manage_users` 交互式按钮增删普通管理员。

---

## 3. 换 IP 模式与核心接口配置

系统通过 `ip_change_provider` 参数切换换 IP 驱动引擎：

```yaml
ip_change_provider: "boil" # 可选: boil 或 classic
```

### A. Boil Network 住宅家宽模式

专为 Boil Network 住宅网络设计的官方直连驱动：

```yaml
ip_change_provider: "boil"
boil_api_base_url: "https://ippanel.boil.network"
boil_api_token: "your_boil_api_token_here"
```

* **工作流程**：
  1. `/check` 检查 IP：请求 `POST /api/v1/getIP`（**不消耗换 IP 额度**），获取当前绑定的公网住宅 IP。
  2. `/change` 更换 IP：请求 `POST /api/v1/changeIP`。
  3. **客户端冷却守护 (`COOLDOWN_PROTECTION`)**：接口返回的 `next_allowed_at` 时间戳会被本地持久化。若用户在冷却期内误点换 IP，Bot 会在本地直接拦截并提示剩余冷却秒数，**彻底杜绝因提前请求被服务商惩罚扣除额外配额**。
  4. 自动提取服务端返回的 `uses_left`（当日剩余可用次数），直观展示在状态报告中。

### B. 经典自建 HTTP API 模式

用于对接各类自建 VPS 控制面板或自定义拨号脚本接口：

```yaml
ip_change_provider: "classic"
ip_change_api: "https://your-panel.example.com/api/change-ip?token=secret123"
ip_change_interval: 2
ip_change_timeout: 600
ip_change_verify_public_ip: true
ip_change_verify_delay: 5
ip_change_retry_verify_count: 3
```

* **参数解析**：
  * `ip_change_api`：换 IP 触发 GET 请求接口。接口应返回 JSON，推荐格式：`{"status": "IP changed", "old_ip": "1.1.1.1", "new_ip": "2.2.2.2"}`。
  * `ip_change_interval`：本地防刷冷却时间（分钟，默认 2 分钟）。
  * `ip_change_verify_public_ip`：换 IP 成功后，是否通过公共 IP 接口轮询校验公网出口确实已发生变更。

---

## 4. 多云厂商 DNS 自动更新配置

换 IP 成功后，Bot 可自动将新公网 IP 同步更新至各大域名托管商。

### 统一 DNS 配置结构

```yaml
dns_update_enabled: true
dns_provider: "cloudflare" # 支持 8 大云厂商
dns_zone_name: "example.com"
dns_record_name: "hkt.example.com"
dns_record_type: "A"       # A (IPv4) 或 AAAA (IPv6)
dns_ttl: 60
```

### 各服务商专属凭据配置

#### 1. Cloudflare
```yaml
dns_provider: "cloudflare"
cloudflare_api_token: "your_cf_api_token_with_zone_dns_permissions"
cloudflare_proxied: false # 家宽直连或代理节点必须设为 false (仅 DNS 解析，不开启小黄云 CDN)
```

#### 2. 华为云 DNS (Huawei Cloud)
```yaml
dns_provider: "huawei"
huawei_ak: "your_huawei_access_key"
huawei_sk: "your_huawei_secret_key"
```

#### 3. 阿里云 DNS (Aliyun)
```yaml
dns_provider: "aliyun"
aliyun_access_key_id: "LTAI5t..."
aliyun_access_key_secret: "your_aliyun_secret"
```

#### 4. 腾讯云 / DNSPod
```yaml
dns_provider: "dnspod" # 或 tencent_dnspod
dnspod_login_token: "123456,abcdef1234567890abcdef1234567890" # 格式通常为 "ID,Token"
```

#### 5. GoDaddy
```yaml
dns_provider: "godaddy"
godaddy_api_key: "your_godaddy_key"
godaddy_api_secret: "your_godaddy_secret"
```

#### 6. Porkbun
```yaml
dns_provider: "porkbun"
porkbun_api_key: "pk1_..."
porkbun_secret_api_key: "sk1_..."
```

#### 7. DigitalOcean
```yaml
dns_provider: "digitalocean"
digitalocean_token: "dop_v1_..."
```

---

## 5. 每日自动换 IP 与递归生效校验

支持在北京时间每天指定时刻自动执行拨号换 IP、同步 DNS、校验域名公网递归生效，并发送包含 IP 质量长图的完整晨报：

```yaml
auto_change_enabled: true
auto_change_time: "04:00"              # 每天自动执行时间 (固定为北京时间 Asia/Shanghai)
auto_change_retry_count: 5            # 换 IP 失败时的自动重试次数
auto_change_retry_delay_seconds: 60   # 重试间隔等待秒数
auto_change_notify: true              # 自动换 IP 结果通知开关
auto_change_quality_report: true      # 换 IP 成功后是否自动生成并发送 /quality 体检卡片

# 递归 DNS 生效验证
dns_verify_enabled: true
dns_verify_delay_seconds: 60          # 换 IP 完成后初次查询公共 DNS 的等待秒数
dns_verify_retry_count: 10            # 轮询公共 DNS 的最大重试次数
```

---

## 6. 远程家宽 SSH 诊断通道与自愈配置

当 Bot 运行在海外轻量云服务器（如 DMIT、AWS、搬瓦工等），而实际代理服务运行在家宽机器（如香港 HKT 家宽）时，可开启本模块：

```yaml
remote_ssh_enabled: true
remote_ssh_host: "hkt.example.com"
remote_ssh_port: 22
remote_ssh_user: "root"
remote_ssh_key_path: "/opt/vps-change-ip/ssh_key.pem"
remote_ssh_password: ""               # 优先使用 key_path，二选一
remote_ssh_timeout: 300
```

### 核心自愈与防污染机制：
1. **强制 1.1.1.1 DoH 解析**：SSH 域名解析强制通过 Cloudflare DNS-over-HTTPS，彻底跳过家庭软路由本地 SmartDNS 或 AdGuardHome 的长 TTL 缓存。
2. **零延迟内存映射注入**：换 IP API 一旦获取到新 IP，立即写入 Bot 进程的内存缓存，SSH 通道立即可用，无需等待任何 DNS 传播。
3. **SSH 255 错误即时自愈**：若远端正在重拨导致 SSH 返回 255 连接中断，Bot 自动触发权威解析刷新，重试建立会话，网络自愈透明无感。

---

## 7. 网络诊断命令微调 (Ping / Quality / Stream)

```yaml
# Ping 延迟检测配置
ping_target: "1.1.1.1"                 # /ping 默认 IPv4 目标
ping_target_v6: "2606:4700:4700::1111" # /ping -6 默认 IPv6 目标 (Cloudflare DNS v6)
ping_count: 10                         # 默认 Ping 报文发送次数

# IP 质量检测 (IP.Check.Place)
ip_quality_enabled: true
ip_quality_cmd: "bash <(curl -sL https://IP.Check.Place) -y"

# 流媒体解锁检测 (1-stream)
stream_check_enabled: true
stream_check_cmd: "bash <(curl -L -s https://github.com/1-stream/RegionRestrictionCheck/raw/main/check.sh)"
stream_check_input: "2"                # 自动交互输入项: 2=跨国+香港, 1=跨国+台湾, 3=日本, 4=北美, 0=仅跨国
stream_check_timeout: 1200
```

---

## 8. 完整样例配置模板

完整现成可用的样例模板请参考项目根目录下的 [`config.yaml.example`](file:///D:/myprojetct/change-ip-bot/config.yaml.example)。
