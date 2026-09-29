# VPS IP Bot 详细配置指南 (Configuration Guide)

本文档提供 `config.yaml` 全部配置项的深度解析、最佳实践与多场景示例。

---

## 目录

1. [配置文件加载与覆盖机制](#1-配置文件加载与覆盖机制)
2. [Telegram 机器人与角色鉴权配置](#2-telegram-机器人与角色鉴权配置)
3. [换 IP 模式与核心接口配置](#3-换-ip-模式与核心接口配置)
   - [A. 通用 API 模式](#a-通用-api-模式-generic默认推荐)
   - [B. Fachost 专用模式](#b-fachost-专用模式-fachost--classic)
   - [C. Boil Network 住宅家宽模式](#c-boil-network-住宅家宽模式-boil)
   - [D. 模式 × 宿主机 / 第三方支持矩阵与选型指南](#d-模式--宿主机--第三方支持矩阵与选型指南)
4. [多云厂商 DNS 自动更新配置](#4-多云厂商-dns-自动更新配置)
5. [每日自动换 IP 与递归生效校验](#5-每日自动换-ip-与递归生效校验)
6. [远程家宽 SSH 诊断通道与自愈配置](#6-远程家宽-ssh-诊断通道与自愈配置)
7. [网络诊断命令微调 (Ping / Quality / Stream)](#7-网络诊断命令微调-ping--quality--stream)
8. [多服务器集群管理配置 (Multi-Server Management)](#8-多服务器集群管理配置-multi-server-management)
9. [完整样例配置模板](#9-完整样例配置模板)

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
ip_change_provider: "generic" # 可选: generic (通用推荐), fachost (Fachost面板), boil (Boil住宅), classic (等同于fachost)
```

### A. 通用 API 模式 (`generic`，默认推荐)

适用于任意软路由（OpenWrt/RouterOS 等 Webhook）、断线重拨脚本、自建面板或第三方换 IP 接口：

```yaml
ip_change_provider: "generic"
ip_change_api: "http://127.0.0.1:8080/reconnect"
ip_change_interval: 2
ip_change_timeout: 60
ip_change_poll_retries: 18 # 探测重试次数（默认18次）
ip_change_poll_delay: 5    # 探测间隔秒数（默认5秒，最多等待90秒）
```

* **工作流程**：
  1. 换 IP 前自动记录当前出口 IPv4。
  2. 发起 GET 请求至 `ip_change_api`（对接口返回体**无任何格式要求**，无论返回 HTML、纯文本、空数据或网卡重启导致中断均可）。
  3. 通过 `curl -4 ip.sb`（多轮轮询）主动探测并捕获最新的公网出口 IP。
  4. 确认 IP 发生变化后，自动执行多平台 DNS 动态同步。

### B. Fachost 专用模式 (`fachost` / `classic`)

专为 **Fachost** 动态 VPS 控制面板定制开发：

```yaml
ip_change_provider: "fachost"
ip_change_api: "https://your-panel.fachost.example.com/api/change-ip?token=secret123"
ip_change_interval: 2
ip_change_timeout: 600
ip_change_verify_public_ip: true
ip_change_verify_delay: 5
ip_change_retry_verify_count: 3
```

* **工作流程**：
  1. 向 Fachost 控制面板发起请求。
  2. 精准解析服务端返回的 JSON 数据（要求 `status: "IP changed"`，并从中提取 `new_ip`）。
  3. 支持出口 IP 校验（`ip_change_verify_public_ip: true`）与请求超时兜底轮询。
* **部署建议**：推荐直接部署在 Fachost VPS 本机，`remote_ssh_enabled: false`。

### C. Boil Network 住宅家宽模式 (`boil`)

专为 **Boil Network** 住宅宽带控制台量身定制的官方直连驱动：

```yaml
ip_change_provider: "boil"
boil_api_base_url: "https://ippanel.boil.network"
boil_api_token: "your_boil_api_token_here"
```

* **部署架构推荐**：**强烈推荐部署于第三方独立海外 VPS**。
  - 换 IP 全程通过公网 REST API 触发，查询 IP 亦走云端接口，**完全解耦**。
  - 配合 `remote_ssh_enabled: true`，网络诊断、测速、流媒体解锁测试借道家宽执行，即便家宽重启/断网，云端 Bot 依然 24 小时保持在线。
* **工作流程**：
  1. `/check` 检查 IP：请求 `POST /api/v1/getIP`（**不消耗换 IP 额度**），获取当前绑定的公网住宅 IP。
  2. `/change` 更换 IP：请求 `POST /api/v1/changeIP`。
  3. **客户端冷却守护 (`COOLDOWN_PROTECTION`)**：接口返回的 `next_allowed_at` 时间戳会被本地持久化。若用户在冷却期内误点换 IP，Bot 会在本地直接拦截并提示剩余冷却秒数，**彻底杜绝因提前请求被服务商惩罚扣除额外配额**。
  4. 自动提取服务端返回的 `uses_left`（当日剩余可用次数），直观展示在状态报告中。

### D. 模式 × 宿主机 / 第三方支持矩阵与选型指南

三种模式在不同部署位置下的行为差异与风险矩阵：

| 换 IP 模式 | 部署位置 | 换 IP API 触发机制 | 获取新 IP 途径 | Bot DDNS 更新支持 | 核心依赖与死锁风险 | 推荐评级 |
| :--- | :--- | :--- | :--- | :---: | :--- | :---: |
| **通用模式<br>(`generic`)** | **宿主机本地**<br>*(家宽/软路由)* | 本地直接调用 API（支持内网 Webhook、光猫重拨、脚本） | 本地执行 `curl -4 ip.sb` 轮询探测 | **✅ 支持**<br>(Bot 自动更新) | **零风险**：单机 100% 本地闭环，无需外部依赖，秒级自愈。 | ⭐⭐⭐⭐⭐<br>**(家宽本机首选)** |
| **通用模式<br>(`generic`)** | **第三方服务器**<br>*(外部 VPS)* | 公网直接请求；内网通过 SSH 隧道穿透触发 | 必须依赖宿主机先被解析，再由 SSH 连入执行 `curl` | **❌ 自动跳过**<br>(由宿主机独立 DDNS 维护) | **高依赖**：宿主机必须自带独立 DDNS（如 `ddns-go`），否则重拨后外部 VPS 无法寻址连入，形成死锁。 | ⭐⭐<br>*(不推荐，易死锁)* |
| **Fachost 模式<br>(`fachost`)** | **宿主机本地**<br>*(Fachost VPS)* | 本地直接请求 Fachost 控制面板 API | 直接解析响应 JSON 中的 `new_ip` | **✅ 支持**<br>(Bot 自动更新) | **零风险**：接口权威下发新 IP，本地双重校验。 | ⭐⭐⭐⭐⭐<br>**(Fachost 首选)** |
| **Fachost 模式<br>(`fachost`)** | **第三方服务器**<br>*(外部 VPS)* | 公网直接请求；内网通过 SSH 隧道穿透触发 | 直接解析响应 JSON 中的 `new_ip` | **❌ 自动跳过**<br>(由宿主机独立 DDNS 维护) | **低风险**：虽知晓新 IP，但为防覆盖宿主机入站域名，Bot 自动跳过 DDNS。 | ⭐⭐⭐<br>*(适合远程驱动测试)* |
| **Boil 住宅模式<br>(`boil`)** | **宿主机本地**<br>*(家宽主机)* | 本地调用 Boil 官方公网云端 REST API | 本地向 Boil 云端接口轮询新分配 IP | **✅ 支持**<br>(Bot 自动更新) | **低风险**：家宽重拨瞬间 Bot 会短暂掉线，但不影响最终同步。 | ⭐⭐⭐⭐<br>*(可用，轻微掉线)* |
| **Boil 住宅模式<br>(`boil`)** | **第三方服务器**<br>*(外部 VPS)* | 外部 VPS 直接调用 Boil 官方公网云端 REST API | 外部 VPS 直接向 Boil 云端轮询新分配 IP | **✅ 支持**<br>(Bot 自动更新) | **零风险**：控制端 24h 永不掉线；云端知晓新 IP 更新 DNS，再借 SSH 跑测试。 | ⭐⭐⭐⭐⭐<br>**(Boil 方案首选)** |

---

## 4. 多云厂商 DNS 自动更新配置

换 IP 成功后，Bot 可自动将新公网 IP 同步更新至各大域名托管商。

> [!IMPORTANT]
> **远程 SSH 模式下的 DDNS 规则**：
> - 当开启远程 SSH（`remote_ssh_enabled: true`）且模式为 `generic` 或 `fachost` 时，Bot 将**自动跳过 DDNS 更新**。因为在此架构下，外部 VPS 依赖远端主机的固定域名/独立 DDNS（如软路由内嵌 DDNS、ddns-go）维持连接，避免覆盖与寻址死锁。
> - 若需要 Bot 自身执行 DDNS 更新，请采用：
>   1. **单机本地部署**（推荐）：直接安装在家宽/软路由本机（`remote_ssh_enabled: false`），由 Bot 本地检测新 IP 并更新 DDNS。
>   2. **Boil 住宅模式**：外部 VPS 部署（`ip_change_provider: "boil"`），通过官方云端 API 权威获取新 IP 并更新 DDNS。

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

## 8. 多服务器集群管理配置 (Multi-Server Management)

当需要由单个 Telegram Bot 同时管理多台 VPS、家庭宽带或不同机房节点时，在 `config.yaml` 中配置 `servers:` 列表。

### A. 配置结构与覆盖继承规则

```yaml
# 根级全局配置（所有节点默认继承这些参数）
telegram_bot_token: "123456789:ABCdefGhIJKlmNoPQRstuvWXyz"
telegram_chat_id: "987654321"
ip_change_provider: "generic"
dns_provider: "cloudflare"
cloudflare_api_token: "global_cf_token"
auto_change_enabled: true
auto_change_time: "04:00"

# 受管节点列表
servers:
  - id: "hkt"                               # 节点唯一标识（英文/数字/下划线）
    name: "香港 HKT 家宽"                   # 节点友好展示名称
    ip_change_provider: "generic"           # 换 IP 模式
    ip_change_api: "http://192.168.1.100:8080/reconnect"
    remote_ssh_enabled: true                # 开启远程 SSH 诊断通道
    remote_ssh_host: "hkt.example.com"
    remote_ssh_port: 22
    remote_ssh_user: "root"
    remote_ssh_key_path: "/opt/vps-change-ip/hkt_ssh.key"
    dns_update_enabled: true
    dns_record_name: "hkt.example.com"
    dns_zone_name: "example.com"
    auto_change_enabled: true
    auto_change_time: "04:00"

  - id: "tokyo"
    name: "东京 VPS 节点"
    ip_change_provider: "generic"
    ip_change_api: "https://api.tokyo-vps.com/change"
    remote_ssh_enabled: false               # 本地直接检测，无需 SSH
    dns_update_enabled: true
    dns_record_name: "tokyo.example.com"
    dns_zone_name: "example.com"
    auto_change_enabled: true
    auto_change_time: "04:30"

  - id: "boil_us"
    name: "美国 Boil 住宅"
    ip_change_provider: "boil"
    boil_api_base_url: "https://ippanel.boil.network"
    boil_api_token: "your_boil_api_token"
    dns_update_enabled: false
    auto_change_enabled: false
```

### B. 关键机制与行为说明

1. **层级继承与局部重写**：
   - 节点中的所有配置项均为**可选覆盖**。未声明的字段（如 `telegram_chat_id`、全局超时时间等）将自动从配置文件根级继承。
   - 每个节点的 `id` 必须全配置唯一，推荐采用简明标识（如 `hkt`、`us1`、`bj_unicom`）。
2. **完全状态隔离与并发防护**：
   - 每个节点在 `state.json` 中以 `servers[server_id]` 隔离存储（独立记录上次 IP、更换状态、Boil 冷却与配额）。
   - 每个节点分配专属的 `asyncio.Lock` 换 IP 锁，多台主机执行换 IP 互不排队阻塞。
3. **独立定时任务注册**：
   - 机器人启动时会自动解析各节点自身的 `auto_change_enabled` 与 `auto_change_time`，并在 JobQueue 中注册独立的每日定时任务（如 HKT 04:00 换，东京 04:30 换）。
4. **100% 单机向后兼容**：
   - 若不配置 `servers:` 字段或列表为空，系统自动工作在单服务器向后兼容模式下，所有命令行为与旧版本完全一致。

---

## 9. 完整样例配置模板

完整现成可用的样例模板请参考项目根目录下的 [`config.yaml.example`](file:///D:/myprojetct/change-ip-bot/config.yaml.example)。

