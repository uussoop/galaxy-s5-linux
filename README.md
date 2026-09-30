# 📱 Galaxy S5 Linux: From E-Waste to Autonomous AI Edge Server

> **Turning a 10-year-old Samsung Galaxy S5 (`SM-G900H` / `k3gxx`) into a headless, native Linux server with 24/7 battery safety management, dual console access, and an autonomous AI agent over Telegram.**

[![OS: postmarketOS / Alpine](https://img.shields.io/badge/OS-postmarketOS%20%2F%20Alpine-003366.svg)](https://postmarketos.org)
[![Kernel: 3.10.9-LineageOS](https://img.shields.io/badge/Kernel-3.10.9--LineageOS-green.svg)](https://github.com/LineageOS)
[![Hardware: Exynos 5410 Octa](https://img.shields.io/badge/Hardware-Exynos%205410%20Octa-blue.svg)](https://en.wikipedia.org/wiki/Exynos#Exynos_5_Octa)
[![Power: 24/7 Store Mode Active](https://img.shields.io/badge/Power-24%2F7%20Store%20Mode%20Active-brightgreen.svg)](#battery-safety--247-plugged-operation)
[![AI Agent: Python 3.14 Smolagents](https://img.shields.io/badge/AI%20Agent-Python%203.14%20Smolagents-blueviolet.svg)](#autonomous-telegram-ai-agent)
[![Documentation: Full Journey](https://img.shields.io/badge/Story-Read%20JOURNEY.md-orange.svg)](JOURNEY.md)

---

## ⚡ What It Does

| Capability | Implementation | Status |
| :--- | :--- | :--- |
| **Boot Architecture** | Native postmarketOS from internal eMMC; **100% Android-free**; zero host dependency | ✅ Verified cold boot |
| **Power Management** | Active MAX77804 PMIC `store_mode` daemon capping charge at **60–70%** for safe 24/7 plugged operation | ✅ Verified 24/7 safe |
| **Serial Console** | Interactive root shell over USB CDC ACM (`/dev/ttyGS0`) at 115200 baud | ✅ Rock-solid lifeline |
| **Framebuffer UI** | Linux console on 1080p Super AMOLED panel (`fbcon`) + `/dev/fb0` status painter (`lvterm`) | ✅ Working |
| **Headless Wi-Fi** | Self-healing BCM4354 orchestrator with automatic roaming & static DHCP acquisition | ✅ Unattended boot |
| **Secure SSH** | Dedicated non-root user (`user`) with passwordless sudo; strictly key-only (`AuthenticationMethods publickey`) | ✅ Hardened |
| **Autonomous AI Agent** | Python 3.14 + Smolagents running local sysfs hardware tools & multi-session LLM routing via Telegram | ✅ Active 24/7 daemon |

---

## 📖 The Complete Story

How did we get here? From resolving device-mapper container partitions, patching CPU-idle crashes in assembly, surviving a phantom volume key, to discovering a 1-line Busybox baud-rate bug that took 26 boot images to solve:

👉 **[Read the Full Engineering Journey in JOURNEY.md](JOURNEY.md)**

---

## 🏗 Architecture & System Stack

```text
 ┌────────────────────────────────────────────────────────────────────────┐
 │                      TELEGRAM INTERFACE (REMOTE)                       │
 │      Private DM  │  Supergroup Forum Topic ('home' thread 4889)        │
 └───────────────────────────────────┬────────────────────────────────────┘
                                     │ HTTPS
                                     ▼
 ┌────────────────────────────────────────────────────────────────────────┐
 │                  AUTONOMOUS AGENT LAYER (/opt/s5-agent)                │
 │  • s5-agent.py (Async Telegram Engine, Strict User Auth, Fast-Path)    │
 │  • s5_agent_core.py (Session State, History Compaction, Topic Policy)  │
 │  • Tool Suite: get_battery_report, manage_wifi, execute_bash, get_file │
 └───────────────────────────────────┬────────────────────────────────────┘
                                     │ Local sysfs & subshells
                                     ▼
 ┌────────────────────────────────────────────────────────────────────────┐
 │                      OPENRC SYSTEM DAEMONS & RUNLEVELS                 │
 │  • s5-charge-limit: Locks PMIC store_mode=1 (60-70% charge threshold)  │
 │  • s5-wifi: WPA Supplicant & udhcpc self-healing network loop          │
 │  • lvterm / fbcon: Super AMOLED framebuffer console painter (/dev/fb0) │
 │  • sshd: Key-only SSH daemon (non-root user, passwordless sudo)        │
 └───────────────────────────────────┬────────────────────────────────────┘
                                     │ POSIX Syscalls
                                     ▼
 ┌────────────────────────────────────────────────────────────────────────┐
 │                 LINUX KERNEL 3.10.9-LineageOS (ARMv7)                  │
 │  • Patched cpu_v7_do_suspend (Stack frame r11 clobber fix)             │
 │  • Samsung DTBH header container (Chip: 0x152e, Platform: 0x1e92)      │
 │  • Device-Mapper linear mapping (dm-0 boot / dm-1 root over mmcblk0p21)│
 │  • Exynos USB composite gadget (acm serial enabled, NCM disabled)      │
 └───────────────────────────────────┬────────────────────────────────────┘
                                     │ Bare Metal
                                     ▼
 ┌────────────────────────────────────────────────────────────────────────┐
 │                  SAMSUNG GALAXY S5 HARDWARE (SM-G900H)                 │
 │  • Exynos 5410 Octa (4x Cortex-A15 @ 1.9GHz + 4x Cortex-A7 @ 1.3GHz)  │
 │  • 2 GB LPDDR3 RAM  │  16 GB eMMC 5.0  │  5.1" Super AMOLED Display    │
 │  • MAX77804 Companion PMIC  │  BCM4354 Dual-Band 802.11ac Wi-Fi/BT     │
 └────────────────────────────────────────────────────────────────────────┘
```

---

## 🤖 Autonomous Telegram AI Agent

The Galaxy S5 runs an integrated autonomous AI agent service (`s5-agent`) supervised by OpenRC:

### 1. Instant Fast-Path Telemetry (<1s Latency, Zero API Cost)
Trivial hardware queries never touch external LLM APIs. Typing `/status`, `status`, `/id`, or `/ping` directly queries hardware sysfs nodes and replies in sub-second time:

```text
📊 System Telemetry

Battery: 68% (Raw SOC: 6834)
Voltage: 3.985 V
Charger State: Discharging
Store Mode (60-70% limit): Active
Temperature: 28.4 °C

Wi-Fi:
=== Galaxy S5 Wi-Fi Status ===
State:       COMPLETED
SSID:        HomeNetwork
IP Address:  192.168.1.100
BSSID:       00:11:22:33:44:55 (2412 MHz)

Uptime:
11:58:38 up  8:17,  0 users,  load average: 0.12, 0.08, 0.05
```

### 2. Native Hardware Tools
When given conversational tasks, the agent uses Smolagents tools to operate the device:
- `get_battery_report()`: Power supply capacity, voltage, raw SOC, charger status, and temperature.
- `manage_wifi(action, target)`: Status, live scanning, and network inspection via `s5-wifi`.
- `execute_bash(command)`: Sandboxed shell execution on Alpine Linux.
- `send_file_to_user(filepath)`: Delivers files from the phone directly into Telegram.

### 3. Multi-Session Memory & Auto-Compaction
- **`/sessions`**: Lists all active conversation contexts with message counts and timestamps.
- **`/session <name>`**: Switches or spawns isolated sessions.
- **`/compact`**: Intelligently summarizes older conversation history to conserve context memory.
- **`/new` / `/reset`**: Re-initializes a fresh session.

### 4. Dynamic Model Switching & Chain-of-Thought
- **`/models` & `/model <id>`**: Switch models on the fly between OpenRouter, OpenAI, and Gemini.
- **`/reasoning on|off`**: Toggles visibility of intermediate model thoughts and tool calls.
- **`/prompt set <text>`**: Live customization of the agent's system prompt.

### 5. Bi-Directional File Transfer
- Upload photos or documents directly in Telegram to stage them in `/opt/s5-agent/downloads/`.
- Download any device file to your chat with `/get <filepath>` (e.g. `/get /var/log/dmesg`).

### 6. Strict Topic & Anti-Interference Isolation
The bot is locked to your specified supergroup (`-1001234567890`) and forum topic (`home` / `thread_id=1234`):
- **Admin ID Only (`<your_telegram_id>`):** Rejects commands from any other user or bot.
- **Mention or Reply Only:** Strictly answers when tagged (`@MeHomyBot /status`) or replied to.
- **Anti-Interference:** Completely ignores unmentioned messages and commands directed to other bots (e.g. `watchman`), allowing you to chat freely without interruptions.

---

## 🔋 Battery Safety & 24/7 Plugged Operation

Running smartphones as servers with standard Android or Linux kernels will inevitably cause **battery swelling** within weeks because the battery is held at 100% (4.35V) permanently.

### The Samsung Store Mode Mechanism
We identified and activated the MAX77804 PMIC driver's built-in battery protection:
```bash
echo 1 > /sys/class/power_supply/battery/store_mode
```
- **Thresholds:** Hardware charging automatically stops when capacity reaches **70%** and resumes only when capacity drops below **60%**.
- **Voltage Safety:** Cell voltage is held between ~3.85V and 4.05V, extending battery lifespan indefinitely.
- **OpenRC Watchdog:** The `s5-charge-limit` daemon activates on boot and monitors battery state in `/var/log/s5-battery.log`.

---

## 💻 How to Connect

### 1. The USB Serial Lifeline (Primary)
The USB port exposes an interactive ACM serial console that survives even if Wi-Fi or SSH breaks:
```bash
# macOS
screen /dev/cu.usbmodem01f44ecab7141 115200

# Linux
screen /dev/ttyACM0 115200
```
*Username: `root` (passwordless on console).*

### 2. Hardened SSH (Wi-Fi)
Available on the local network once the phone associates with Wi-Fi:
```bash
ssh -i work/galaxy-s5/ssh/s5_agent_ed25519 user@<phone-ip>
```
- Root SSH login is refused (`PermitRootLogin no`).
- User `user` has passwordless `sudo` rights.
- Authentication strictly requires Ed25519 public key.

---

## 📂 Repository Structure

```text
galaxy-s5-linux/
├── README.md                      # Showcase documentation & overview
├── JOURNEY.md                     # Deep-dive 11-chapter engineering narrative
├── AGENTS.md                      # Operational rules & workspace constraints
├── CHANGELOG.md                   # Real-time modification ledger
├── HANDOFF.md                     # Quick operational guide
├── outputs/
│   └── Test S5.command            # One-click read-only Mac SSH test script
└── work/galaxy-s5/
    ├── CURRENT-STATE.md           # Granular hardware & partition state
    ├── progress.md                # Chronological build log (r1 – r26)
    ├── artifacts/
    │   ├── boot-k3gxx-r26.img     # The verified working boot image
    │   ├── SHA256SUMS.txt         # Checksums of all historical artifacts
    │   └── BUILD-MANIFEST.md      # Kernel and initramfs manifests
    ├── scripts/
    │   ├── s5-agent.py            # Async Telegram agent engine & fast-path router
    │   ├── s5_agent_core.py       # Session manager, history compactor, group policy
    │   ├── test_agent_features.py # Automated unit test suite
    │   ├── s5-wifi.sh             # Wi-Fi orchestrator & self-healing daemon
    │   ├── serial-drive.py        # Automated USB serial command driver
    │   ├── serial-watch.py        # Passive serial output monitor
    │   └── usb-descriptor-watch.py# Passive USB device connection watcher
    ├── lvterm/                    # Framebuffer console source & LVGL patches
    ├── diagnostics/               # Sanitized kernel logs, dmesg, and boot traces
    └── ssh/                       # Pinned host keys and authorized public keys
```

---

## 🛠 Rebuilding & Reproducing

### 1. Kernel & Initramfs Prerequisites
- **Toolchain:** GCC cross-compiler for `armv7-alpine-linux-musleabihf` (with `KCFLAGS=-std=gnu89`).
- **Source:** LineageOS downstream kernel `android_kernel_samsung_k3gxx` (branch `cm-11.0`, commit `2f78dde45cd08d9f1f7f047c81ffb237f7ab9b56`).
- **DTBH Tool:** `dtbTool-exynos` to pack Samsung-tagged DTB container (Chip `0x152e`, Platform `0x1e92`).

### 2. Device-Mapper Linear Tables (`/etc/init.d/s5-rootmount`)
Mount nested subpartitions without triggering loopback direct-I/O panics:
```bash
dmsetup create dm-0 --table "0 497664 linear /dev/mmcblk0p21 2048"
dmsetup create dm-1 --table "0 24346624 linear /dev/mmcblk0p21 499712"
```

### 3. The Working `/etc/inittab` Serial Line
Always specify the baud rate before the tty name for Busybox `getty`:
```text
ttyGS0::respawn:/sbin/getty -L 115200 ttyGS0 vt100
```

---

## ⚠️ Hard Rules & Lessons Learned

1. **Never Re-Enable USB NCM:** Toggling NCM network state tears down the USB composite gadget, killing the serial console.
2. **Never Restore `.s5bak` Files:** Old backup files like `/etc/inittab.s5bak` carry the broken getty line that eliminates serial access on next boot.
3. **Always Keep Store Mode Active:** Never disable `store_mode` while connected to a continuous power supply.
4. **Physical Buttons are Manual:** Entering Download Mode (`VolDown + Home + Power`) or TWRP (`VolUp + Home + Power`) requires manual physical key presses.

---

## 📜 License & Acknowledgments

- **Kernel:** GPLv2 (Linux / LineageOS / Samsung).
- **Userspace:** Alpine Linux / postmarketOS.
- Built with dedication to e-waste revival, open hardware exploration, and autonomous edge computing.
