<div align="center">

<img src="public/logo.png" alt="VPS IP Bot" width="130" />

# **VPS IP Bot**

### Automated IP Switching · Multi-Provider DNS Synchronization · Remote SSH Dual-Stack Diagnostics

[![Release](https://img.shields.io/github/v/release/violetaini/change-ip-bot?color=blue&style=flat-square)](https://github.com/violetaini/change-ip-bot/releases)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)
[![Telegram](https://img.shields.io/badge/Telegram-Bot%20API-0088cc?style=flat-square&logo=telegram&logoColor=white)](https://core.telegram.org/bots/api)
[![Tests](https://img.shields.io/badge/Tests-46%20Passed-brightgreen?style=flat-square)](tests/test_all.py)

**[English](README.md)** · **[简体中文](README_CN.md)**

</div>

> 💡 **Special Support**: Out-of-the-box optimized support for **Fachost** (Classic HTTP Change-IP API / Dynamic VPS) and **Boil Network** (Cloud Console API / Residential Broadband), supporting both standalone local deployments and Cloud-to-Edge split architectures.

---

- 📖 **[Detailed Configuration Guide & Reference (docs/CONFIGURATION.md)](docs/CONFIGURATION.md)**
- 🔄 **[IP Switching Modes & Deployment Architecture](#-ip-switching-modes--deployment-architecture)**
- ⚡ **[Quick Start & Installation](#-quick-start)**
- 🤖 **[Telegram Command Reference](#-command-reference)**
- 🗑️ **[Uninstallation & Service Cleanup](#️-uninstallation)**

---

## 📖 Overview

**VPS IP Bot** is an enterprise-grade Telegram automation bot engineered for VPS proxy servers and dynamic residential broadband (PPPoE redial nodes).

It solves critical pain points in dynamic IP management: client-side quota & cooldown protection, instant multi-cloud DNS synchronization, local DNS cache poisoning avoidance, and accurate dual-stack (IPv4 & IPv6) diagnostic probing through firewalls.

---

## ✨ Key Features

- 🔄 **Dual-Mode IP Switching Architecture**:
  - **Classic Mode (`classic`)**: Seamlessly connects to custom VPS panel or provider HTTP change-IP APIs.
  - **Boil Network Mode (`boil`)**: Direct official API integration for Boil Network residential IPs (`changeIP` & `getIP`).
- 🛡️ **Client-Side Cooldown & Quota Guard**:
  - Validates Boil Network's server timestamp (`next_allowed_at`) locally. Intercepts premature requests before hitting the API, preventing wasted daily change quotas.
- 🌐 **Multi-Provider Automated DNS Synchronization**:
  - Automatically updates configured DNS records (A / AAAA) within seconds after IP rotation.
  - Built-in support for **8 major DNS providers**: Cloudflare, Aliyun, Tencent Cloud / DNSPod, Huawei Cloud, GoDaddy, Porkbun, DigitalOcean.
  - Recursive public DNS propagation verification after scheduled automatic changes.
- ⚡ **Remote SSH Diagnostics & 1.1.1.1 DoH Self-Healing**:
  - **Separation of Concerns**: The bot can run on a lightweight overseas VPS and execute diagnostics on remote residential machines via SSH. Heavy rendering (Cairo/SVG) is performed on the bot host.
  - **Direct 1.1.1.1 DoH Resolution**: Bypasses local DNS caches (SmartDNS, AdGuard Home) by resolving domains directly through Cloudflare DNS-over-HTTPS.
  - **Zero-Delay DNS Memory Mapping**: Authoritative new IPs are immediately injected into memory caches upon rotation. Auto-recovers from SSH error 255 on IP change.
- 🇨🇳 **True GFW Border Penetration Verification**:
  - Eliminates false positives from traditional tests (e.g. `itdog.cn` resolving to overseas Cloudflare Anycast POPs).
  - Uses **EDNS Client Subnet (ECS)** with domestic IP ranges to resolve `v.qq.com` (Tencent Video) to true mainland Chinese China Telecom CDN nodes.
  - Performs multi-attempt HTTP connection probes with strict timeouts to verify genuine border traversal.
- 📶 **Full-Stack Dual-Stack (IPv4 / IPv6) Adaptation**:
  - **`/quality` (IP Quality)**: Fetches and groups IPv4 and IPv6 SVG reports into a combined Telegram media group album. Supports `-4` / `-6` flags; gracefully degrades on single-stack nodes.
  - **`/stream` (Streaming Unlock)**: Decouples IPv4 and IPv6 test blocks, preserving ISP/ASN metadata for both stacks with region selection.
  - **`/ping` (Latency Test)**: Supports `-4`, `-6`, auto-detection of IPv6 colons, and `-c` packet counts.
  - **`/speedtest` (Bandwidth Test)**: Ookla Speedtest output clearly annotates the client external IP and stack type (`IPv4` / `IPv6`).
- 🔒 **Role-Based Access Control & Privacy Redaction**:
  - Distinguishes Super Admins from Regular Admins for sensitive operations.
  - Globally redacts API tokens, private keys, and passwords (`<redacted>`) across all logs and messages.

---

## 🏗️ Architecture

```text
                  Telegram Client (iOS / Android / Desktop)
                                │
                                ▼  (Telegram Bot API)
            ┌───────────────────────────────────────────────┐
            │               VPS IP Bot Core                 │
            │  - Command Parsing & RBAC Authorization       │
            │  - Persistent State & Cooldown Lock           │
            │  - 1.1.1.1 DoH & Zero-Delay Local DNS Cache  │
            │  - Cairo / SVG Report Rendering Engine        │
            └──────────┬─────────────────┬──────────────────┘
                       │                 │
      (Change IP API)  │                 │ (Dynamic DNS Update)
                       ▼                 ▼
          ┌──────────────────────┐  ┌─────────────────────────────────┐
          │  Boil Network API    │  │  Cloudflare / Huawei / Aliyun   │
          │  Custom VPS Panel    │  │  DNSPod / GoDaddy / Porkbun etc │
          └──────────────────────┘  └─────────────────────────────────┘
                       │ (PPPoE Redial)
                       ▼
          ┌───────────────────────────────────────────────────────────┐
          │              Remote Residential Host (via SSH)            │
          │  - Dual-Stack IPv4 & Public IPv6 Networking               │
          │  - Secure SSH Command Execution Pipeline                  │
          │  - True Mainland GFW Traversal Probe (v.qq.com CDN)       │
          │  - IP.Check.Place Quality / Stream Unlock / Speedtest     │
          └───────────────────────────────────────────────────────────┘
```

---

## 🔄 IP Switching Modes & Deployment Architecture

The bot supports two distinct operational modes designed for different network topologies:

### 1. Classic Mode (`classic`) —— Specially Optimized for Fachost / Dynamic VPS & Soft Routers
- **Mechanism**: Calls a local or LAN HTTP endpoint (e.g. Fachost panel change-IP API, router WebHook, or local redial script).
- **Verification Loop**: After triggering rotation, the bot queries public IP APIs (`ipify`, etc.) from the **host machine itself** to verify external IP change.
- **Deployment Location**: **Must/Recommended to run locally on the node changing IP**.
  - *Rationale*: External third-party servers cannot reach internal LAN endpoints directly, and a remote VPS's external IP remains static, causing IP change verification to fail.
- **SSH Setting**: Keep disabled (`remote_ssh_enabled: false`, default). Everything executes locally with zero SSH configuration needed.

### 2. Boil Network Mode (`boil`) —— Deeply Integrated for Boil Residential Broadband
- **Mechanism**: Calls **Boil Network's official public Cloud REST API** (`https://ippanel.boil.network`) directly to trigger rotation.
- **Verification Loop**: Authoritative IP allocation is queried directly from Boil's cloud API, **completely independent of the bot host's local egress IP**.
- **Deployment Location**: **Natively supports deployment on third-party standalone overseas VPS** (Recommended).
  - *Architectural Benefit*: Run the bot 24/7 on an ultra-stable cloud VPS (avoiding Telegram downtime when residential PPPoE resets). When running network tests (`/quality`, `/stream`, `/speedtest`), the bot tunnels commands via SSH into the residential node to measure real residential metrics.
- **SSH Setting**: Enabled (`remote_ssh_enabled: true`) on remote VPS; can also be disabled (`false`) if installed directly on the residential node.

### Deployment Topology Comparison

| Deployment Target | Recommended Provider / Machine | IP Change Mode | SSH State (`remote_ssh_enabled`) | Architectural Highlights |
| :--- | :--- | :---: | :---: | :--- |
| **Installed Locally on Target Node** | **Fachost** / Soft Router / Local Host | **Classic (`classic`)** or Boil | **Disabled** (`false`) | 100% local closed loop, zero SSH keys or port mapping required. |
| **Installed on Independent VPS** | **Boil Network** Residential Host | **Boil (`boil`)** | **Enabled** (`true`) | Cloud-controlled rotation + SSH-tunneled network diagnostics. |

---

## 🤖 Command Reference

| Command | Arguments | Permission | Description |
| :--- | :--- | :---: | :--- |
| `/start` | None | Public | Show welcome message and available commands |
| `/check` | None | Admin | Check current public IP, dual-stack addresses & GFW domestic reachability |
| `/change` | None | Admin | Trigger manual IP change and sync configured DNS records |
| `/ip_status` | None | Admin | Inspect current IP mode, cooldown timer, and remaining quota |
| `/quality` | `[-4 / -6]` | Admin | Generate high-res IP quality card (dual-stack album or single-stack photo) |
| `/stream` | `[region]` | Admin | Run streaming unlock check (`2`=HK+Global, `1`=TW, `0`=Global) |
| `/ping` | `[-4/-6] [target] [-c count]` | Admin | Test network latency with IPv4/IPv6 target auto-detection |
| `/speedtest`| None | Admin | Interactive Ookla Speedtest with client egress IP & protocol stack |
| `/health` | None | Admin | Check bot system CPU, memory, disk, and dependencies status |
| `/set_ip_mode` | `[classic / boil]` | Super Admin | Switch IP change provider dynamically via buttons or arguments |
| `/set_boil_token`| `<token>` | Super Admin | Set and reload Boil API token |
| `/set_ip_api` | `<url>` | Super Admin | Set Classic HTTP change-IP API endpoint URL |
| `/auto_start` | None | Super Admin | Enable daily scheduled automatic IP rotation |
| `/auto_stop` | None | Super Admin | Disable daily scheduled automatic IP rotation |
| `/auto_status`| None | Admin | View scheduled automatic change status and next trigger time |
| `/set_auto_time`| `HH:MM` | Super Admin | Set daily automatic change time in Beijing Time (`04:00`) |
| `/manage_users`| None | Super Admin | Interactive button-based admin user management (Add/Remove) |
| `/logs` | `[lines]` | Super Admin | View recent bot logs with automatic credential redaction |
| `/dns_status` | None | Super Admin | Inspect current dynamic DNS configuration |
| `/set_dns_provider` | `<provider>` | Super Admin | Set DNS provider (`cloudflare`, `huawei`, `aliyun`, etc.) |
| `/set_dns_record` | `ZONE RECORD [TYPE] [TTL]` | Super Admin | Configure DNS zone, record name, type, and TTL |
| `/dns_update_on` | None | Super Admin | Enable automatic DNS updates after IP change |
| `/dns_update_off`| None | Super Admin | Disable automatic DNS updates after IP change |

---

## 🚀 Quick Start

### 1. Prerequisites & System Dependencies

Recommended OS: Ubuntu 20.04+ or Debian 11+.
Install Python, Cairo graphics libraries, and fonts for SVG report generation:

```bash
apt update
apt install -y python3 python3-pip python3-venv curl libcairo2 fonts-wqy-zenhei
```

*(Optional: If `chromium` is installed, the bot will prioritize Chromium headless rendering for `/quality` reports. If not available, it cleanly falls back to CairoSVG).*

### 2. Installation

You can install via direct GitHub static asset download (no Git required) or via Git clone:

#### Option A: Direct Download via wget / curl (Recommended, no Git required)

```bash
mkdir -p /opt/vps-change-ip
cd /opt/vps-change-ip

# Directly download and unpack GitHub tarball using wget:
wget -qO- https://github.com/violetaini/change-ip-bot/archive/refs/heads/main.tar.gz | tar -zxvf - --strip-components=1

# Or download zip archive:
# wget -O main.zip https://github.com/violetaini/change-ip-bot/archive/refs/heads/main.zip && unzip -o main.zip && cp -r change-ip-bot-main/* . && rm -rf change-ip-bot-main main.zip
```

#### Option B: Clone via Git (Convenient for future git pull updates)

```bash
mkdir -p /opt/vps-change-ip
cd /opt/vps-change-ip
git clone https://github.com/violetaini/change-ip-bot.git .
```

#### Setup Virtual Environment & Install Dependencies

```bash
cd /opt/vps-change-ip
python3 -m venv venv
source venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

### 3. Configuration

> [!TIP]
> For comprehensive parameter explanations, 8 DNS cloud providers setups, and anti-abuse cooldown mechanisms, please see:
> 👉 **[Detailed Configuration Guide & Reference (docs/CONFIGURATION.md)](docs/CONFIGURATION.md)**

Copy the example configuration:

```bash
cp config.yaml.example config.yaml
nano config.yaml
```

**Minimal Essential Settings**:

```yaml
telegram_bot_token: "123456789:ABCdefGhIJKlmNoPQRstuvWXyz"
telegram_chat_id: "987654321"

# Access Control (Supports user ID whitelist)
telegram_super_admin_user_ids: "987654321"
telegram_admin_user_ids: ""

# Select IP Change Mode
ip_change_provider: "boil" # or classic
boil_api_base_url: "https://ippanel.boil.network"
boil_api_token: "your_boil_api_token"

# DNS Auto-Sync (Example: Cloudflare)
dns_update_enabled: true
dns_provider: "cloudflare"
dns_zone_name: "example.com"
dns_record_name: "hkt.example.com"
dns_record_type: "A"
cloudflare_api_token: "your_cf_api_token"

# Remote SSH Execution (Optional: Run diagnostics on remote residential machine)
remote_ssh_enabled: true
remote_ssh_host: "hkt.example.com"
remote_ssh_port: 22
remote_ssh_user: "root"
remote_ssh_key_path: "/opt/vps-change-ip/ssh_key.pem"
```

### 4. Running Manually

Run in foreground to test:

```bash
python src/bot.py
```

Send `/start` and `/check` in Telegram to verify bot functionality.

---

## 🛠️ Systemd Service Setup

Create `/etc/systemd/system/vps-ip-bot.service`:

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

Enable and start the service:

```bash
systemctl daemon-reload
systemctl enable vps-ip-bot
systemctl start vps-ip-bot
systemctl status vps-ip-bot --no-pager
```

View live logs:

```bash
journalctl -u vps-ip-bot -f
```

---

## 🗑️ Uninstallation

To completely remove VPS IP Bot from your server, follow these steps:

### 1. Stop and Remove the Systemd Service

```bash
# Stop the running service and disable auto-start
systemctl stop vps-ip-bot
systemctl disable vps-ip-bot

# Remove service unit file and reload daemon
rm -f /etc/systemd/system/vps-ip-bot.service
systemctl daemon-reload
systemctl reset-failed
```

### 2. Remove Application Directory & Data

```bash
# Delete application directory (virtualenv, code, and config)
rm -rf /opt/vps-change-ip

# Remove runtime persistent state directory (if applicable)
rm -rf /var/lib/vps-ip-bot
```

### One-Line Complete Uninstall Command

```bash
systemctl stop vps-ip-bot && systemctl disable vps-ip-bot && rm -f /etc/systemd/system/vps-ip-bot.service && systemctl daemon-reload && rm -rf /opt/vps-change-ip /var/lib/vps-ip-bot
```

---

## 🧪 Testing

The repository includes a comprehensive unit test suite covering configuration validation, credential redaction, state management, multi-provider routing, zero-delay SSH self-healing, EDNS domestic reachability, and dual-stack formatters:

```bash
python -m unittest tests/test_all.py
```

Test status: **46 unit tests passing (100% pass rate)**.

---

## 🔒 Security Best Practices

1. **Automatic Credential Redaction**: All Telegram logs, `/logs` command output, and error traces pass through `redact_text` to sanitize API tokens and passwords.
2. **Key Security**: Keep your SSH private keys protected with `chmod 600`.
3. **Repository Cleanliness**: `.gitignore` strictly ignores `config.yaml`, `*.pem`, `*.key`, `id_rsa`, and local state files. Never commit private credentials.

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
Contributions, issues, and feature requests are welcome!
