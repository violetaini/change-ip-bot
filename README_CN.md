<div align="center">

<img src="public/logo.png" alt="VPS IP Bot" width="130" />

# **VPS IP Bot**

### 专为 VPS 与住宅家宽打造的自动化换 IP · 多服务商 DNS 动态同步 · 远程 SSH 双栈网络诊断机器人

[![Release](https://img.shields.io/github/v/release/violetaini/change-ip-bot?color=blue&style=flat-square)](https://github.com/violetaini/change-ip-bot/releases)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)
[![Telegram](https://img.shields.io/badge/Telegram-Bot%20API-0088cc?style=flat-square&logo=telegram&logoColor=white)](https://core.telegram.org/bots/api)
[![Tests](https://img.shields.io/badge/Tests-46%20Passed-brightgreen?style=flat-square)](tests/test_all.py)

**[English](README.md)** · **[简体中文](README_CN.md)**

</div>

> 💡 **特别支持**：开箱深度优化并完美支持 **Fachost**（经典 HTTP 换 IP 接口 / 动态 VPS）与 **Boil Network**（云端控制台 API / 住宅宽带），支持单机单节点部署与“云端控制 + 边缘家宽”远程分离部署。

---

- 📖 **[详细配置指南与最佳实践 (docs/CONFIGURATION_CN.md)](docs/CONFIGURATION_CN.md)**
- 🔄 **[换 IP 模式与部署架构选型 (Classic vs. Boil)](#-换-ip-模式与部署架构选型)**
- ⚡ **[快速安装与部署说明](#-快速开始)**
- 🤖 **[Telegram 完整命令列表](#-telegram-命令一览)**
- 🗑️ **[卸载与服务清理说明](#️-卸载与完全清理)**

---

## 📖 项目简介

**VPS IP Bot** 是一款面向 VPS 代理服务器与住宅家宽（PPPoE 动态拨号主机）的 Telegram 全功能自动化运维管理机器人。

项目致力于解决动态 IP 服务器运维中的核心痛点：换 IP 过程的配额保护、DNS 动态记录的实时同步、本地 DNS 缓存对新 IP 的解析延迟，以及对双栈（IPv4 / IPv6）和真实境内（GFW）穿透连通性的精准诊断。

---

## ✨ 核心特性

- 🔄 **三模式换 IP 架构 (Triple-Engine Architecture)**：
  - **通用模式 (`generic`，默认推荐)**：发起换 IP API 请求（支持各类软路由 Webhook、重拨脚本、控制台 URL），随后自动通过 `curl -4 ip.sb` 轮询获取新 IP，不强制接口返回特定格式，适用面最广。
  - **Fachost 专用模式 (`fachost` / `classic`)**：专为 **Fachost** 动态 VPS 控制面板定制，精准解析响应 JSON（`status: "IP changed"`, `new_ip`）及出口双重校验。
  - **Boil Network 住宅家宽模式 (`boil`)**：官方 API 直连集成（`changeIP` 换 IP & `getIP` 静默查 IP），自带客户端冷却守护与每日配额追踪。
- 🛡️ **客户端配额与冷却守护 (Cooldown Protection)**：
  - 智能记录并严格校验 Boil 服务端 `next_allowed_at` 冷却时间戳，客户端侧拦截提前误触，杜绝浪费宝贵的每日换 IP 次数。
- 🌐 **多主流云厂商 DNS 自动化同步**：
  - 换 IP 成功后秒级自动同步指定解析记录（支持 A / AAAA）。
  - 已支持 **8 大主流 DNS 平台**：Cloudflare、阿里云 (Aliyun)、腾讯云 / DNSPod、华为云 (Huawei Cloud)、GoDaddy、Porkbun、DigitalOcean。
  - 定时自动换 IP 流程中支持公共 DNS（递归解析）生效轮询校验。
- ⚡ **远程 SSH 诊断与 1.1.1.1 DoH 零延迟自愈**：
  - **轻重分离架构**：机器人本体运行于海外轻量 VPS，通过 SSH 远端静默执行诊断，渲染与通知在本地完成，无需在家宽机器安装繁重依赖。
  - **强制 1.1.1.1 DoH 解析**：SSH 域名解析强制通过 Cloudflare DNS-over-HTTPS，彻底规避本地 SmartDNS、AdGuardHome 等局域网缓存对新 IP 的污染。
  - **零延迟 DNS 内存映射更新**：触发换 IP 后，API 获取到的最新公网 IP 立即注入本地解析缓存；即便遭遇 SSH 连接错误（255），也会自动探测 IP 变更并即时重试，实现网络自愈。
- 🇨🇳 **真实穿越 GFW 境内连通性检测**：
  - 彻底摒弃传统“伪境内连通性测试”（如 `itdog.cn` 实际命中 Cloudflare 海外 Anycast 节点导致的误报）。
  - 采用 **EDNS Client Subnet (ECS)** 携带境内运营商网段解析 `v.qq.com`（腾讯视频）获取真实境内骨干网 IP。
  - 通过远端向该境内 IP 发起多轮重试（默认 3 次）的 TCP 握手 + TLS + HTTP 响应全链路探测，准确判定是否真实被 GFW 阻断。
- 📶 **全链路单双栈（IPv4 / IPv6）深度适配**：
  - **`/quality` (IP 质量体检)**：自动抓取 IPv4 与 IPv6 双图报告，打包 Media Group 发送，支持 `-4` / `-6` 指定检测；纯单栈机器自动降级为单图。
  - **`/stream` (流媒体解锁检测)**：自动拆分 IPv4 与 IPv6 结果区块，保留各自 ASN 信息并结构化呈现；支持地区快捷选择。
  - **`/ping` (延迟测试)**：支持 `-4`、`-6`、冒号智能推导 IPv6、`-c` 计数；单栈不可达时优雅回显。
  - **`/speedtest` (宽带测速)**：Ookla Speedtest 测速卡片清晰标明客户端实际出口 IP 及协议栈（`IPv4` / `IPv6`）。
- 🔒 **严格安全与隐私脱敏**：
  - 角色鉴权（超级管理员 vs 普通管理员），敏感指令与管理操作严格隔离。
  - 全局日志及消息对 Token、密码、私钥等敏感凭据自动执行 `<redacted>` 脱敏，保障开源与共享安全。

---

## 🏗️ 架构拓扑

```text
                  Telegram Client (手机 / 电脑)
                                │
                                ▼  (Telegram Bot API)
            ┌───────────────────────────────────────────────┐
            │               VPS IP Bot 核心进程             │
            │  - 指令解析与角色鉴权 (Super Admin / Admin)    │
            │  - 状态持久化与冷却锁 (state.json)            │
            │  - 1.1.1.1 DoH 域名直接解析与内存 DNS 映射   │
            │  - Cairo / SVG 图片报表渲染引擎               │
            └──────────┬─────────────────┬──────────────────┘
                       │                 │
      (换 IP 接口 / API)│                 │ (动态 DNS 自动更新)
                       ▼                 ▼
          ┌──────────────────────┐  ┌─────────────────────────────────┐
          │  Boil Network API    │  │  Cloudflare / 华为云 / 阿里云   │
          │  Fachost / 通用 API  │  │  DNSPod / GoDaddy / Porkbun 等  │
          └──────────────────────┘  └─────────────────────────────────┘
                       │ (PPPoE 重新拨号)
                       ▼
          ┌───────────────────────────────────────────────────────────┐
          │              远程住宅家宽主机 (Remote Residential Host)   │
          │  - IPv4 + 公网 IPv6 双栈网络                             │
          │  - 远程 SSH 命令执行通道                                  │
          │  - 境内直连 GFW 穿透连通性探测 (v.qq.com 电信节点)         │
          │  - IP.Check.Place 质量检测 / 流媒体解锁 / Ookla Speedtest │
          └───────────────────────────────────────────────────────────┘
```

---

## 🔄 换 IP 模式与部署架构选型

系统支持三种换 IP 驱动引擎，适应不同的部署环境：

### 1. 通用模式 (`generic`，默认推荐) —— 适用任意自建脚本/软路由
- **换 IP 机制**：向用户配置的 `ip_change_api` 发起 GET 请求（如软路由重连 URL、自定义 Webhook、面板重拨接口），对接口返回格式**无任何要求**。
- **获取新 IP 机制**：API 触发后，Bot 自动在后台通过 `curl -4 ip.sb`（多轮重试轮询）主动探测并捕获最新的公网 IPv4 出口，完成新 IP 确认与 DNS 自动同步。
- **部署位置**：直接安装在目标机本机（关闭 SSH）或安装在第三方独立服务器（开启 SSH 代理执行 `curl -4 ip.sb`）。

### 2. Fachost 专用模式 (`fachost` / `classic`) —— 专为 Fachost 动态 VPS 定制
- **换 IP 机制**：向 Fachost 控制面板的换 IP API 发起请求。
- **获取新 IP 机制**：直接解析服务端返回的特定 JSON 数据体（提取 `new_ip` 与 `status == "IP changed"`），并带有双重校验与超时出口轮询兜底。
- **部署位置**：**必须/推荐直接安装在换 IP 机器本机**（保持 `remote_ssh_enabled: false`）。

### 3. Boil 优化模式 (`boil`) —— 专为 Boil 住宅宽带深度定制
- **换 IP 机制**：由 Bot 请求 **Boil 官方公网云端 REST API** (`https://ippanel.boil.network`)，下发换 IP 任务。
- **获取新 IP 机制**：换 IP 后直接向 Boil 云端控制台轮询新分配的 IP，**完全不依赖本地出口**。
- **部署位置**：**天然支持部署在第三方独立海外 VPS 上**（推荐架构）。
  - *架构优势*：将 Bot 部署在稳定的海外 VPS 上，24 小时保持在线（避免家宽断网导致 Bot 失联）；通过可选的远程 SSH 直连家宽节点，在测速、流媒体解锁、IP 质量检测时借道家宽物理出口，实现“云端控制中枢 + 家宽物理执行”的轻重分离。
- **SSH 状态**：装在第三方 VPS 时开启（`remote_ssh_enabled: true`）；若直接装在家宽本机，亦可关闭 SSH 纯本地运行。

### 部署架构矩阵对照 (宿主机 vs 第三方服务器)

| 换 IP 模式 | 部署位置 | 换 IP API 触发机制 | 获取新 IP 途径 | Bot DDNS 更新支持 | 核心依赖与死锁风险 | 推荐评级 |
| :--- | :--- | :--- | :--- | :---: | :--- | :---: |
| **通用模式<br>(`generic`)** | **宿主机本地**<br>*(家宽/软路由)* | 本地直接调用 API（支持内网 Webhook、光猫重拨、脚本） | 本地执行 `curl -4 ip.sb` 轮询探测 | **✅ 支持**<br>(Bot 自动更新) | **零风险**：单机 100% 本地闭环，无需外部依赖，秒级自愈。 | ⭐⭐⭐⭐⭐<br>**(家宽本机首选)** |
| **通用模式<br>(`generic`)** | **第三方服务器**<br>*(外部 VPS)* | 公网直接请求；内网通过 SSH 隧道穿透触发 | 必须依赖宿主机先被解析，再由 SSH 连入执行 `curl` | **❌ 自动跳过**<br>(由宿主机独立 DDNS 维护) | **高依赖**：宿主机必须自带独立 DDNS（如 `ddns-go`），否则重拨后外部 VPS 无法寻址连入，形成死锁。 | ⭐⭐<br>*(不推荐，易死锁)* |
| **Fachost 模式<br>(`fachost`)** | **宿主机本地**<br>*(Fachost VPS)* | 本地直接请求 Fachost 控制面板 API | 直接解析响应 JSON 中的 `new_ip` | **✅ 支持**<br>(Bot 自动更新) | **零风险**：接口权威下发新 IP，本地双重校验。 | ⭐⭐⭐⭐⭐<br>**(Fachost 首选)** |
| **Fachost 模式<br>(`fachost`)** | **第三方服务器**<br>*(外部 VPS)* | 公网直接请求；内网通过 SSH 隧道穿透触发 | 直接解析响应 JSON 中的 `new_ip` | **❌ 自动跳过**<br>(由宿主机独立 DDNS 维护) | **低风险**：虽知晓新 IP，但为防覆盖宿主机入站域名，Bot 自动跳过 DDNS。 | ⭐⭐⭐<br>*(适合远程驱动测试)* |
| **Boil 住宅模式<br>(`boil`)** | **宿主机本地**<br>*(家宽主机)* | 本地调用 Boil 官方公网云端 REST API | 本地向 Boil 云端接口轮询新分配 IP | **✅ 支持**<br>(Bot 自动更新) | **低风险**：家宽重拨瞬间 Bot 会短暂掉线，但不影响最终同步。 | ⭐⭐⭐⭐<br>*(可用，轻微掉线)* |
| **Boil 住宅模式<br>(`boil`)** | **第三方服务器**<br>*(外部 VPS)* | 外部 VPS 直接调用 Boil 官方公网云端 REST API | 外部 VPS 直接向 Boil 云端轮询新分配 IP | **✅ 支持**<br>(Bot 自动更新) | **零风险**：控制端 24h 永不掉线；云端知晓新 IP 更新 DNS，再借 SSH 跑测试。 | ⭐⭐⭐⭐⭐<br>**(Boil 方案首选)** |

> 💡 **部署选型建议**：
> 1. **家宽软路由 / 局域网断线重拨**：选 **`generic` (通用模式)**，直接部署在宿主机本地，关闭 SSH 并开启 Bot DDNS，单机单节点闭环最省心。
> 2. **Fachost 动态 VPS**：选 **`fachost` 模式**，直接部署在机子本机，关闭 SSH，自动提取 JSON 换 IP 并更新 DDNS。
> 3. **Boil 住宅宽带**：选 **`boil` 模式**，部署在第三方海外轻量 VPS 上，开启 SSH，控制端 24h 在线，云端换 IP 查 IP 并更新 DDNS，网络测试借道家宽。

---

## 🤖 Telegram 命令一览

| 命令 | 参数 | 权限要求 | 功能说明 |
| :--- | :--- | :---: | :--- |
| `/start` | 无 | 普通用户 | 显示欢迎信息与当前可用命令菜单 |
| `/check` | 无 | 管理员 | 检测当前公网 IP、双栈地址及穿透 GFW 境内连通性 |
| `/change` | 无 | 管理员 | 手动触发更换 IP，并自动同步更新配置的云厂商 DNS |
| `/ip_status` | 无 | 管理员 | 查看当前换 IP 模式、冷却剩余时间及每日剩余配额 |
| `/quality` | `[-4 / -6]` | 管理员 | 生成高质量 IP 体检报告图（双栈自动合并为相册发送） |
| `/stream` | `[地区编号]` | 管理员 | 检测流媒体解锁情况（`2`=跨国+香港, `1`=台湾, `0`=仅跨国） |
| `/ping` | `[-4/-6] [目标] [-c 次数]` | 管理员 | 测试网络延迟，支持指定目标、次数或直接输入 IPv6 |
| `/speedtest`| 无 | 管理员 | 交互式选择节点进行 Ookla 测速，展示出口 IP 栈类型 |
| `/health` | 无 | 管理员 | 检查 Bot 所在系统 CPU、内存、磁盘及依赖就绪状态 |
| `/set_ip_mode` | `[generic/fachost/boil]` | 超级管理员 | 交互式切换换 IP 模式（通用 / Fachost / Boil） |
| `/set_boil_token`| `<token>` | 超级管理员 | 在线设置并热重载 Boil API Token |
| `/set_ip_api` | `<url>` | 超级管理员 | 在线设置换 IP 请求接口 URL |
| `/auto_start` | 无 | 超级管理员 | 开启每日定时自动换 IP 任务 |
| `/auto_stop` | 无 | 超级管理员 | 关闭每日定时自动换 IP 任务 |
| `/auto_status`| 无 | 管理员 | 查看自动换 IP 运行状态及下次执行时间 |
| `/set_auto_time`| `HH:MM` | 超级管理员 | 设置每日自动换 IP 时间（北京时间，如 `04:00`） |
| `/manage_users`| 无 | 超级管理员 | 按钮式交互管理普通管理员名单（增加/删除） |
| `/logs` | `[行数]` | 超级管理员 | 查看 Bot 最近运行日志（敏感信息已脱敏） |
| `/dns_status` | 无 | 超级管理员 | 查看当前 DNS 自动同步配置信息 |
| `/set_dns_provider` | `<服务商>` | 超级管理员 | 设置 DNS 服务商（如 `cloudflare`、`huawei` 等） |
| `/set_dns_record` | `ZONE RECORD [TYPE] [TTL]` | 超级管理员 | 配置域名托管 Zone、解析主机记录及 TTL |
| `/dns_update_on` | 无 | 超级管理员 | 启用换 IP 后自动更新 DNS |
| `/dns_update_off`| 无 | 超级管理员 | 暂停换 IP 后自动更新 DNS |

---

## 🚀 快速开始

### 1. 系统要求与基础依赖

推荐运行在 Ubuntu 20.04+ / Debian 11+ 系统。
报告图片渲染依赖系统图形库与中文字体支持：

```bash
apt update
apt install -y python3 python3-pip python3-venv curl libcairo2 fonts-wqy-zenhei
```

*(可选：若宿主机安装了 `chromium`，机器人将优先使用 Chromium 无头截图生成 `/quality` 报告，未安装时自动回退为 Python 内置 CairoSVG 渲染)*

### 2. 部署与安装

提供两种安装方式，任选其一即可：

#### 方式 A：直接下载 GitHub 静态源码包（无需安装 Git，推荐）

```bash
mkdir -p /opt/vps-change-ip
cd /opt/vps-change-ip

# 使用 wget 直接下载并解包 GitHub 静态资源 (tar.gz)
wget -qO- https://github.com/violetaini/change-ip-bot/archive/refs/heads/main.tar.gz | tar -zxvf - --strip-components=1

# 或使用 zip 压缩包下载解压：
# wget -O main.zip https://github.com/violetaini/change-ip-bot/archive/refs/heads/main.zip && unzip -o main.zip && cp -r change-ip-bot-main/* . && rm -rf change-ip-bot-main main.zip
```

#### 方式 B：通过 Git 仓库克隆（便于后续 git pull 升级）

```bash
mkdir -p /opt/vps-change-ip
cd /opt/vps-change-ip
git clone https://github.com/violetaini/change-ip-bot.git .
```

#### 创建 Python 虚拟环境并安装依赖

```bash
cd /opt/vps-change-ip
python3 -m venv venv
source venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

### 3. 配置参数

> [!TIP]
> 完整参数深度解析、8 大云厂商 DNS 专属密钥配置及客户端防刷保护说明，请参阅：
> 👉 **[详细配置指南与最佳实践 (docs/CONFIGURATION_CN.md)](docs/CONFIGURATION_CN.md)**

复制配置模版：

```bash
cp config.yaml.example config.yaml
nano config.yaml
```

**最简必备配置项**：

```yaml
telegram_bot_token: "123456789:ABCdefGhIJKlmNoPQRstuvWXyz"
telegram_chat_id: "987654321"

# 权限分配（支持按用户ID严格鉴权）
telegram_super_admin_user_ids: "987654321"
telegram_admin_user_ids: ""

# 选择换 IP 模式
ip_change_provider: "boil" # 或 classic
boil_api_base_url: "https://ippanel.boil.network"
boil_api_token: "your_boil_api_token"

# DNS 自动同步 (以 Cloudflare 为例)
dns_update_enabled: true
dns_provider: "cloudflare"
dns_zone_name: "example.com"
dns_record_name: "hkt.example.com"
dns_record_type: "A"
cloudflare_api_token: "your_cf_api_token"

# 远程家宽 SSH 诊断通道 (可选，若直接在目标机运行则保持 false)
remote_ssh_enabled: true
remote_ssh_host: "hkt.example.com"
remote_ssh_port: 22
remote_ssh_user: "root"
remote_ssh_key_path: "/opt/vps-change-ip/ssh_key.pem"
```

### 4. 运行与验证

在虚拟环境中启动前台测试：

```bash
python src/bot.py
```

在 Telegram 中向 Bot 发送 `/start` 和 `/check` 验证响应。

---

## 🛠️ 配置为 Systemd 守护进程

创建 Systemd 服务文件 `/etc/systemd/system/vps-ip-bot.service`：

```ini
[Unit]
Description=VPS IP Bot Service
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

重载并启动服务：

```bash
systemctl daemon-reload
systemctl enable vps-ip-bot
systemctl start vps-ip-bot
systemctl status vps-ip-bot --no-pager
```

查看实时运行日志：

```bash
journalctl -u vps-ip-bot -f
```

---

## 🗑️ 卸载与完全清理

如需从服务器彻底移除 VPS IP Bot，执行以下步骤即可：

### 1. 停止并移除 Systemd 服务

```bash
# 停止运行中的服务并禁用自启
systemctl stop vps-ip-bot
systemctl disable vps-ip-bot

# 移除服务单元文件并重载配置
rm -f /etc/systemd/system/vps-ip-bot.service
systemctl daemon-reload
systemctl reset-failed
```

### 2. 清理工作目录与持久化数据

```bash
# 删除程序运行目录（虚拟环境、代码与本地配置）
rm -rf /opt/vps-change-ip

# 删除运行时状态数据目录（如有）
rm -rf /var/lib/vps-ip-bot
```

### 一键卸载指令

```bash
systemctl stop vps-ip-bot && systemctl disable vps-ip-bot && rm -f /etc/systemd/system/vps-ip-bot.service && systemctl daemon-reload && rm -rf /opt/vps-change-ip /var/lib/vps-ip-bot
```

---

## 🧪 自动化测试

项目内置了完备的自动化单元测试集，包含配置加载、脱敏安全、状态机、多云 DNS、远程 SSH 零延迟重试自愈、EDNS 境内穿透探测、流媒体与测速双栈格式化等：

```bash
python -m unittest tests/test_all.py
```

当前测试覆盖：**46 项用例全部通过**。

---

## 🔒 安全与隐私实践

1. **凭证脱敏保障**：Bot 内置脱敏引擎 `redact_text`，所有 Telegram 交互日志、`/logs` 输出及异常回显中的 Bearer Token、SSH 密码、Bot Token 均会被替换为 `<redacted>`。
2. **私钥隔离**：远程 SSH 私钥文件建议设置 `chmod 600 /path/to/key.pem`。
3. **敏感文件防泄露**：`.gitignore` 默认排除了 `config.yaml`、`*.pem`、`*.key`、`id_rsa` 及运行时状态文件，严禁将私密配置提交至公共仓库。

---

## 📄 开源许可证

本项目基于 [MIT License](LICENSE) 协议开源。
欢迎提交 Issue 与 Pull Request 共同改进！
