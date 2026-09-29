# VPS IP Bot Configuration Guide

This document provides a comprehensive reference, parameter breakdown, and configuration examples for `config.yaml`.

---

## Table of Contents

1. [Configuration Loading & Overrides](#1-configuration-loading--overrides)
2. [Telegram Bot & Role-Based Access Control](#2-telegram-bot--role-based-access-control)
3. [IP Change Providers](#3-ip-change-providers)
   - [A. Generic API Mode](#a-generic-api-mode-generic-default--recommended)
   - [B. Fachost Dedicated Mode](#b-fachost-dedicated-mode-fachost--classic)
   - [C. Boil Network Residential IP Mode](#c-boil-network-residential-ip-mode-boil)
   - [D. Provider Mode × Host / Third-Party Support Matrix](#d-provider-mode--host--third-party-support-matrix--recommendations)
4. [Multi-Cloud DNS Auto-Sync](#4-multi-cloud-dns-auto-sync)
5. [Scheduled Automated Changes & Propagation Verification](#5-scheduled-automated-changes--propagation-verification)
6. [Remote Residential SSH Tunnel & Self-Healing](#6-remote-residential-ssh-tunnel--self-healing)
7. [Network Diagnostics Tuning (Ping / Quality / Stream)](#7-network-diagnostics-tuning-ping--quality--stream)
8. [Full Example Template](#8-full-example-template)

---

## 1. Configuration Loading & Overrides

- **File Path**: The bot reads `config.yaml` from the application working directory on startup. If missing, default baseline settings are applied.
- **State File (`state_file`)**:
  - Persists cooldown timestamps, remaining daily quotas, notification states, and scheduled jobs.
  - Default: `/var/lib/vps-ip-bot/state.json`.
  - **Environment Override**: You can override the path using the `VPS_IP_BOT_STATE_FILE` environment variable (e.g., `VPS_IP_BOT_STATE_FILE=/custom/path/state.json`).
- **Security Notice**: `config.yaml` contains API credentials, Bot tokens, and SSH keys. It is ignored by `.gitignore` by default. **Never commit secrets to public repositories.**

---

## 2. Telegram Bot & Role-Based Access Control

```yaml
telegram_bot_token: "123456789:ABCdefGhIJKlmNoPQRstuvWXyz"
telegram_chat_id: "-1001234567890,987654321"
telegram_allowed_user_ids: ""
telegram_super_admin_user_ids: "987654321"
telegram_admin_user_ids: "11223344,55667788"
```

| Parameter | Type | Required | Default | Description |
| :--- | :---: | :---: | :---: | :--- |
| `telegram_bot_token` | string | **Yes** | `""` | Telegram Bot API Token from [@BotFather](https://t.me/BotFather). |
| `telegram_chat_id` | string | **Yes** | `""` | Allowed Chat ID(s), separated by commas. Can be group IDs (negative numbers) or user IDs. |
| `telegram_allowed_user_ids` | string | No | `""` | Legacy user whitelist fallback. |
| `telegram_super_admin_user_ids` | string | Recommended | `""` | **Super Admin User ID(s)**. Has full access to scheduled tasks, provider modes, tokens, user management, and logs. |
| `telegram_admin_user_ids` | string | No | `""` | **Regular Admin User ID(s)**. Allowed to execute `/change`, `/check`, and read-only diagnostic commands. |

> [!TIP]
> **Permission Levels**:
> - If both `telegram_super_admin_user_ids` and `telegram_admin_user_ids` are empty, legacy authorization by `telegram_chat_id` or `telegram_allowed_user_ids` applies.
> - Super Admins can manage regular admins in real time using the `/manage_users` interactive button interface.

---

## 3. IP Change Providers

Select the provider engine via `ip_change_provider`:

```yaml
ip_change_provider: "generic" # Options: generic (recommended), fachost (Fachost panel), boil (Boil residential), classic (alias for fachost)
```

### A. Generic API Mode (`generic`, Default & Recommended)

Designed for arbitrary soft routers (OpenWrt/RouterOS webhooks), redial scripts, custom control panels, or generic change-IP APIs:

```yaml
ip_change_provider: "generic"
ip_change_api: "http://127.0.0.1:8080/reconnect"
ip_change_interval: 2
ip_change_timeout: 60
ip_change_poll_retries: 18 # Number of poll attempts (default: 18)
ip_change_poll_delay: 5    # Delay between polls in seconds (default: 5s, total 90s)
```

* **Workflow**:
  1. Records current external IPv4 prior to rotation.
  2. Dispatches an HTTP GET request to `ip_change_api` (**zero response formatting requirements**; works even if connection drops due to interface restart).
  3. Actively queries and polls `curl -4 ip.sb` to discover the new public egress IP.
  4. Automatically synchronizes DNS records upon new IP verification.

### B. Fachost Dedicated Mode (`fachost` / `classic`)

Specially tailored for **Fachost** dynamic VPS control panels:

```yaml
ip_change_provider: "fachost"
ip_change_api: "https://your-panel.fachost.example.com/api/change-ip?token=secret123"
ip_change_interval: 2
ip_change_timeout: 600
ip_change_verify_public_ip: true
ip_change_verify_delay: 5
ip_change_retry_verify_count: 3
```

* **Workflow**:
  1. Dispatches request to the Fachost control panel.
  2. Parses the returned JSON payload (`status: "IP changed"`, `new_ip`).
  3. Features host egress verification and timeout fallback polling.
* **Deployment**: Recommended to install directly on the Fachost VPS (`remote_ssh_enabled: false`).

### C. Boil Network Residential IP Mode (`boil`)

Official API driver specifically engineered for **Boil Network** dynamic residential broadband:

```yaml
ip_change_provider: "boil"
boil_api_base_url: "https://ippanel.boil.network"
boil_api_token: "your_boil_api_token_here"
```

* **Recommended Architecture**: **Highly recommended to deploy on a third-party overseas cloud VPS**.
  - IP rotation and status querying execute completely via public cloud REST APIs (**fully decoupled**).
  - Paired with `remote_ssh_enabled: true`, network tests, speedtest, and streaming unlock tests tunnel through the residential host. Even if residential PPPoE drops, the cloud bot stays 24/7 online.
* **Workflow**:
  1. `/check`: Queries `POST /api/v1/getIP` (**does not consume change quotas**) to retrieve current residential IP.
  2. `/change`: Triggers `POST /api/v1/changeIP`.
  3. **Client-Side Cooldown Guard (`COOLDOWN_PROTECTION`)**: The API-returned `next_allowed_at` timestamp is persisted locally. If an IP change is attempted during cooldown, the bot blocks the request locally, **preventing accidental quota deduction penalties**.
  4. Automatically parses `uses_left` (remaining daily quota) and includes it in status summaries.

### D. Provider Mode × Host / Third-Party Support Matrix & Recommendations

Behavioral and operational matrix across deployment topologies:

| Provider Mode | Deployment Location | Change IP Trigger Mechanism | New IP Discovery Loop | Bot DDNS Auto-Sync | Dependencies & Deadlock Risk | Recommendation Rating |
| :--- | :--- | :--- | :--- | :---: | :--- | :---: |
| **Generic Mode<br>(`generic`)** | **Local Host**<br>*(Broadband/Router)* | Direct local API call (LAN Webhook, redial script) | Local `curl -4 ip.sb` polling loop | **✅ Supported**<br>(Bot auto-syncs) | **Zero Risk**: 100% standalone closed loop, zero external dependencies. | ⭐⭐⭐⭐⭐<br>**(Recommended for Router)** |
| **Generic Mode<br>(`generic`)** | **Third-Party Server**<br>*(Cloud VPS)* | Public URL directly; LAN URL tunneled via SSH | Requires host domain resolved before SSH `curl` | **❌ Auto-Skipped**<br>(Managed by host DDNS) | **High Dependency**: Host must have separate DDNS (e.g. `ddns-go`); otherwise SSH deadlocks. | ⭐⭐<br>*(Deadlock prone)* |
| **Fachost Mode<br>(`fachost`)** | **Local Host**<br>*(Fachost VPS)* | Direct call to Fachost control panel API | Direct parsing of `new_ip` from JSON response | **✅ Supported**<br>(Bot auto-syncs) | **Zero Risk**: Authoritative response with local verification. | ⭐⭐⭐⭐⭐<br>**(Recommended for Fachost)** |
| **Fachost Mode<br>(`fachost`)** | **Third-Party Server**<br>*(Cloud VPS)* | Public URL directly; LAN URL tunneled via SSH | Direct parsing of `new_ip` from JSON response | **❌ Auto-Skipped**<br>(Managed by host DDNS) | **Low Risk**: New IP is known, but Bot skips DDNS to prevent overwriting host domain. | ⭐⭐⭐<br>*(Good for remote tests)* |
| **Boil Residential<br>(`boil`)** | **Local Host**<br>*(Residential Node)* | Direct call to Boil official cloud REST API | Polling new IP from Boil cloud console API | **✅ Supported**<br>(Bot auto-syncs) | **Low Risk**: Temporary disconnection during PPPoE redial; resumes automatically. | ⭐⭐⭐⭐<br>*(Functional)* |
| **Boil Residential<br>(`boil`)** | **Third-Party Server**<br>*(Cloud VPS)* | VPS directly calls Boil official cloud REST API | VPS directly polls new IP from Boil cloud API | **✅ Supported**<br>(Bot auto-syncs) | **Zero Risk**: VPS controller stays 24/7 online; updates DNS, tests tunnel via SSH. | ⭐⭐⭐⭐⭐<br>**(Recommended for Boil)** |

---

## 4. Multi-Cloud DNS Auto-Sync

Automatically updates DNS records upon IP rotation:

> [!IMPORTANT]
> **DDNS Behavior under Remote SSH Mode**:
> - When Remote SSH is enabled (`remote_ssh_enabled: true`) and provider is `generic` or `fachost`, the bot **automatically skips DDNS updates**. In this topology, the remote node must maintain its own independent DDNS (e.g. built-in router DDNS, ddns-go) to prevent domain overwrites and SSH deadlocks.
> - To have the bot manage DDNS updates, choose one of the following topologies:
>   1. **Local Standalone Deployment** (Recommended): Deploy directly on target node (`remote_ssh_enabled: false`) with `generic` or `fachost`.
>   2. **Boil Cloud Mode**: Deploy on external VPS (`ip_change_provider: "boil"`), querying new IPs authoritatively via Boil cloud API.

### Common DNS Structure

```yaml
dns_update_enabled: true
dns_provider: "cloudflare" # Supports 8 cloud providers
dns_zone_name: "example.com"
dns_record_name: "hkt.example.com"
dns_record_type: "A"       # A (IPv4) or AAAA (IPv6)
dns_ttl: 60
```

### Provider Credentials

#### 1. Cloudflare
```yaml
dns_provider: "cloudflare"
cloudflare_api_token: "your_cf_api_token_with_zone_dns_permissions"
cloudflare_proxied: false # Must be false for direct proxy node connections (DNS only)
```

#### 2. Huawei Cloud DNS
```yaml
dns_provider: "huawei"
huawei_ak: "your_huawei_access_key"
huawei_sk: "your_huawei_secret_key"
```

#### 3. Aliyun DNS
```yaml
dns_provider: "aliyun"
aliyun_access_key_id: "LTAI5t..."
aliyun_access_key_secret: "your_aliyun_secret"
```

#### 4. Tencent Cloud / DNSPod
```yaml
dns_provider: "dnspod" # or tencent_dnspod
dnspod_login_token: "123456,abcdef1234567890abcdef1234567890" # "ID,Token" format
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

## 5. Scheduled Automated Changes & Propagation Verification

Run automated daily IP rotation at a specified Beijing time, sync DNS, verify public recursive propagation, and send comprehensive quality reports:

```yaml
auto_change_enabled: true
auto_change_time: "04:00"              # Daily execution time (Asia/Shanghai)
auto_change_retry_count: 5            # Retry attempts on failure
auto_change_retry_delay_seconds: 60   # Delay between retries
auto_change_notify: true              # Send Telegram notification on change
auto_change_quality_report: true      # Automatically run /quality after change

# Recursive DNS Verification
dns_verify_enabled: true
dns_verify_delay_seconds: 60          # Initial delay before checking public DNS
dns_verify_retry_count: 10            # Maximum polling attempts
```

---

## 6. Remote Residential SSH Tunnel & Self-Healing

When the bot runs on a cloud VPS while your actual proxy service runs on a residential broadband machine (e.g. Hong Kong HKT), enable this tunnel:

```yaml
remote_ssh_enabled: true
remote_ssh_host: "hkt.example.com"
remote_ssh_port: 22
remote_ssh_user: "root"
remote_ssh_key_path: "/opt/vps-change-ip/ssh_key.pem"
remote_ssh_password: ""               # Key authentication preferred
remote_ssh_timeout: 300
```

### Self-Healing & Anti-Poisoning Highlights:
1. **Direct 1.1.1.1 DoH Resolution**: Bypasses local SmartDNS or AdGuardHome caches by querying Cloudflare DNS-over-HTTPS directly.
2. **Instant Memory Mapping Injection**: Newly acquired IPs from the change-IP API are immediately injected into the bot's memory DNS mapping.
3. **SSH Error 255 Instant Recovery**: If SSH drops (code 255) during router PPPoE redial, the bot automatically refreshes authoritative DNS and retries the session.

---

## 7. Network Diagnostics Tuning (Ping / Quality / Stream)

```yaml
# Ping Latency Configuration
ping_target: "1.1.1.1"                 # Default IPv4 ping target
ping_target_v6: "2606:4700:4700::1111" # Default IPv6 ping target (Cloudflare DNS v6)
ping_count: 10                         # Packet count

# IP Quality Probing (IP.Check.Place)
ip_quality_enabled: true
ip_quality_cmd: "bash <(curl -sL https://IP.Check.Place) -y"

# Streaming Unlock Probing (1-stream)
stream_check_enabled: true
stream_check_cmd: "bash <(curl -L -s https://github.com/1-stream/RegionRestrictionCheck/raw/main/check.sh)"
stream_check_input: "2"                # Auto input: 2=Global+HK, 1=Global+TW, 3=JP, 4=NA, 0=Global only
stream_check_timeout: 1200
```

## 8. Multi-Server Management Configuration

When managing multiple VPS, residential broadband, or cloud nodes from a single bot, configure the `servers:` list in `config.yaml`.

### A. Structure & Override Rules

```yaml
# Root-level configuration (all nodes inherit defaults from here)
telegram_bot_token: "123456789:ABCdefGhIJKlmNoPQRstuvWXyz"
telegram_chat_id: "987654321"
ip_change_provider: "generic"
dns_provider: "cloudflare"
cloudflare_api_token: "global_cf_token"
auto_change_enabled: true
auto_change_time: "04:00"

# Managed nodes list
servers:
  - id: "hkt"                               # Unique server identifier (letters/numbers/underscores)
    name: "Hong Kong HKT Residential"       # User-friendly display name
    ip_change_provider: "generic"           # IP switching engine
    ip_change_api: "http://192.168.1.100:8080/reconnect"
    remote_ssh_enabled: true                # Enable SSH diagnostics tunnel
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
    name: "Tokyo VPS Node"
    ip_change_provider: "generic"
    ip_change_api: "https://api.tokyo-vps.com/change"
    remote_ssh_enabled: false               # Local node, no SSH needed
    dns_update_enabled: true
    dns_record_name: "tokyo.example.com"
    dns_zone_name: "example.com"
    auto_change_enabled: true
    auto_change_time: "04:30"

  - id: "boil_us"
    name: "US Boil Residential Node"
    ip_change_provider: "boil"
    boil_api_base_url: "https://ippanel.boil.network"
    boil_api_token: "your_boil_api_token"
    dns_update_enabled: false
    auto_change_enabled: false
```

### B. Core Mechanisms

1. **Inheritance & Local Overrides**:
   - Any property omitted within a server item automatically inherits the root-level configuration.
   - Each server `id` must be globally unique.
2. **State Isolation & Concurrency Guard**:
   - Node state is partitioned under `state["servers"][server_id]` (isolated last IP, change status, Boil quota/cooldown).
   - Each node possesses its own `asyncio.Lock` change-guard so rotations execute concurrently without blocking.
3. **Independent Scheduled Jobs**:
   - Separate daily cron jobs are scheduled per server according to its `auto_change_enabled` and `auto_change_time`.
4. **100% Backward Compatibility**:
   - If `servers:` is omitted or empty, the bot seamlessly operates in single-server mode with identical legacy behavior.

---

## 9. Full Example Template

See [`config.yaml.example`](file:///D:/myprojetct/change-ip-bot/config.yaml.example) for the complete, ready-to-use template.

