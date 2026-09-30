# The Journey: Reviving a 2014 Galaxy S5 into an Autonomous Linux Edge Node

> *"Twenty-six boot images, dozens of silent hangs, a phantom volume button, an assembly register clobber, and a single missing baud-rate argument. This is the complete technical story of turning a 10-year-old Samsung Galaxy S5 into an autonomous Linux AI node."*

---

## Table of Contents
1. [The Challenge & The Hardware](#1-the-challenge--the-hardware)
2. [Phase 1: Booting Native Linux from eMMC (r1 – r10)](#2-phase-1-booting-native-linux-from-emmc-r1--r10)
3. [Phase 2: The Nested Partition & Device-Mapper Puzzle](#3-phase-2-the-nested-partition--device-mapper-puzzle)
4. [Phase 3: The Framebuffer Display & The Phantom Key (r11 – r17)](#4-phase-3-the-framebuffer-display--the-phantom-key-r11--r17)
5. [Phase 4: The 26-Image Hunt for a Working Console (r18 – r26)](#5-phase-4-the-26-image-hunt-for-a-working-console-r18--r26)
6. [Phase 5: The USB Gadget Dilemma (Why USB NCM Had to Die)](#6-phase-5-the-usb-gadget-dilemma-why-usb-ncm-had-to-die)
7. [Phase 6: Standalone Headless Wi-Fi Orchestration](#7-phase-6-standalone-headless-wi-fi-orchestration)
8. [Phase 7: Hardware Power Safety & The "Store Mode" Breakthrough](#8-phase-7-hardware-power-safety--the-store-mode-breakthrough)
9. [Phase 8: Building the Autonomous Edge AI Agent](#9-phase-8-building-the-autonomous-edge-ai-agent)
10. [Phase 9: Telegram Bot API vs. MTProto Protocol Investigation](#10-phase-9-telegram-bot-api-vs-mtproto-protocol-investigation)
11. [Key Lessons Learned & Hard Rules](#11-key-lessons-learned--hard-rules)

---

## 1. The Challenge & The Hardware

Smartphones from 2014 are usually destined for landfills or dark drawers. The **Samsung Galaxy S5 (SM-G900H / `k3gxx`)** is particularly notorious in the postmarketOS and Linux-on-mobile communities:
* **SoC:** Samsung Exynos 5410 Octa (4× Cortex-A15 @ 1.9 GHz + 4× Cortex-A7 @ 1.3 GHz big.LITTLE architecture).
* **RAM:** 2 GB LPDDR3.
* **Storage:** 16 GB eMMC 5.0.
* **Display:** 5.1" Super AMOLED (1080×1920).
* **Modem / PMIC:** MAX77804 companion PMIC + proprietary Samsung baseband.
* **Bootloader:** Strictly locked down, Samsung proprietary ABOOT format, non-standard Device Tree Blob (DTBH) header with Samsung platform tags (`0x152e`, `0x1e92`, `0x7d64f612`).

Unlike the Qualcomm Snapdragon variant (SM-G900F / `klte`), the Exynos variant (`k3gxx`) was **archived and abandoned** in upstream `pmaports` with the note: *"Kernel does not build anymore"*. There were no modern prebuilt boot images, no working mainline kernel, and no verified Wi-Fi or battery management.

The mission was uncompromising:
1. **100% Android Removal:** Erase Android completely and boot native Alpine Linux / postmarketOS from the internal eMMC.
2. **Headless & Autonomous:** Boot unattended without a host computer, automatically join Wi-Fi, and survive reboots with all configurations intact.
3. **Safe 24/7 Power:** Prevent battery swelling when plugged into USB power continuously.
4. **Autonomous AI Node:** Run a modern Python 3 agent on the device with hardware telemetry, system management, and remote interaction via Telegram.

---

## 2. Phase 1: Booting Native Linux from eMMC (r1 – r10)

The first hurdle was getting a compiled downstream kernel (`3.10.9-LineageOS`) to compile under modern GCC 15 and survive the Samsung bootloader handoff.

### Breakthrough 1: Modern GCC 15 Compilation
Modern GCC treats old C89 kernel idioms as fatal errors. We introduced `KCFLAGS=-std=gnu89` into the APKBUILD and fixed missing FIPS helper prototypes in the crypto subsystem.

### Breakthrough 2: DTBH Identification
Samsung's bootloader rejects any kernel whose appended Device Tree Blob (DTB) does not carry Samsung's proprietary DTBH header matching the exact board revision:
```text
Chip: 0x152e | Platform: 0x1e92 | Subtype: 0x7d64f612 | Revision: 10
```
Generating the correct DTBH container using `dtbTool-exynos` finally allowed the Samsung bootloader to execute the Linux kernel.

### Breakthrough 3: The Assembly Register Clobber in `cpu_v7_do_suspend`
Even with the correct DTB, the kernel consistently crashed when the CPU attempted to enter idle states. Forensic analysis of the kernel crash dump revealed:
* In `arch/arm/mm/proc-v7.S`, `cpu_v7_do_suspend` was clobbering callee-saved register `r11`.
* Modern GCC-compiled callers relied on `r11` for frame pointer calculations. Upon returning from suspend, the caller dereferenced invalid memory address `0xffffffe4`.
* **Fix:** A precise two-instruction patch preserving and restoring `r11` on the stack:
```assembly
push {r11, lr}
/* ... suspend sequence ... */
pop {r11, pc}
```
With this fix (kernel `r1`), the kernel stayed alive continuously with zero panics.

---

## 3. Phase 2: The Nested Partition & Device-Mapper Puzzle

Once the kernel booted, it repeatedly failed at 7–16 seconds with:
```text
Mount subpartitions of /dev/mmcblk0p21
Buffer I/O error on device loop0, logical block 0
ERROR: failed to mount subpartitions!
```

### The Root Cause: Partitions Inside Partitions
The Galaxy S5 partition table (`stock-partitions.pit`) allocates `/dev/block/mmcblk0p21` as the `USERDATA` partition. To avoid re-partitioning the physical eMMC (which risks hard-bricking Samsung bootloaders), the postmarketOS installer installs a nested MBR partition table *inside* `p21`:
- Subpartition 1 (`p1`): 256 MiB `ext2` boot partition (starts at sector 2048).
- Subpartition 2 (`p2`): ~11.6 GiB `ext4` root partition (starts at sector 499712).

The initramfs originally attempted to scan this with:
```bash
losetup --show -Pf --direct-io=on /dev/mmcblk0p21
```
The Linux 3.10 block layer could not negotiate direct I/O on loop devices backed by the eMMC device node, producing persistent `Buffer I/O error on loop0`.

### The Solution: Device-Mapper Linear Tables
Instead of relying on fragile loopback auto-partitioning, we implemented deterministic **device-mapper linear tables**:
```bash
# Map boot partition (p1) directly over p21 sectors:
echo "0 497664 linear /dev/mmcblk0p21 2048" | dmsetup create dm-0

# Map root partition (p2) directly over p21 sectors:
echo "0 24346624 linear /dev/mmcblk0p21 499712" | dmsetup create dm-1
```
`dm-0` (`/boot`) and `dm-1` (`/`) mounted instantaneously with zero I/O errors.

---

## 4. Phase 3: The Framebuffer Display & The Phantom Key (r11 – r17)

Even though Linux was running, the 5.1" Super AMOLED display remained pitch black.

### Enabling the Framebuffer Console (`fbcon`)
The downstream kernel configuration had `# CONFIG_FRAMEBUFFER_CONSOLE is not set`. We rebuilt the kernel (`linux-samsung-k3gxx-3.10.9-r2`) enabling `CONFIG_FRAMEBUFFER_CONSOLE=y` and `CONFIG_FONTS=y`. 

On reboot, the kernel logged:
```text
Console: switching to colour frame buffer device 135x120
```
Tux and early boot text appeared crisply on the phone's 1080p panel.

### The Phantom Volume Key Hang
At round `r14`, boot halted inside the initramfs (`init_2nd.sh:40`). The initramfs had a recovery-mode trigger that checked for hardware keys. On this phone, `gpio_keys.16` reported a stuck `KEY_VOLUMEUP` state on every cold boot, causing the initramfs to halt and wait for user input on a non-existent USB keyboard.

We implemented `s5keys`, a dedicated filter that intercepted the phantom keypress event and allowed unattended boot to proceed without human intervention.

---

## 5. Phase 4: The 26-Image Hunt for a Working Console (r18 – r26)

This was the most grueling phase of the project. We had a booting system and display output, but **no input path**:
- The touchscreen driver (`synaptics_dsx`) required Android proprietary daemon initialization and was dead in native Linux.
- The phone had no hardware keyboard.
- SSH was not yet established.
- If the serial console failed, there was zero way to enter commands or inspect logs.

From round `r18` to `r25`, we produced image after image trying to make the USB CDC ACM serial console (`/dev/ttyGS0`) provide an interactive login shell. Each round booted, printed early messages, and then went completely silent at exactly **2 minutes 52 seconds**.

We suspected everything:
- Device tree clock gates?
- USB PHY suspend?
- CPU frequency scaling governor crashes?
- Android composite gadget driver bugs?

### The Eureka Moment: The 1-Line Busybox Bug
In round `r26`, we audited the exact command line in `/etc/inittab`:
```text
# What was in place:
ttyGS0::respawn:/sbin/getty -L ttyGS0 vt100
```

Notice the order of arguments:
In GNU/sysvinit `getty`, the syntax is `getty [options] line [baud_rate]`.
However, in **Busybox `getty`**, the syntax is:
```text
Usage: getty [OPTIONS] BAUD_RATE TTY [TERMTYPE]
```

Because the baud rate was missing, Busybox parsed the string `"ttyGS0"` as the **baud rate**! It then looked for baud rate `ttyGS0`, failed, and exited with:
```text
getty: bad speed: vt100
```
Inittab respawned it. It failed again immediately. After 10 rapid respawns, inittab rate-limited it and fell completely silent!

### The One-Line Fix:
```text
ttyGS0::respawn:/sbin/getty -L 115200 ttyGS0 vt100
```
We inserted `115200` between `-L` and `ttyGS0`. 

We plugged the USB cable into our Mac, ran `screen /dev/cu.usbmodem01f44ecab7141 115200`, pressed Enter, and were greeted with:
```text
galaxy-s5 login: root
Welcome to postmarketOS!
galaxy-s5:~# uname -a
Linux galaxy-s5 3.10.9-LineageOS #1 PREEMPT armv7l Linux
```
Twenty-six rounds of debugging to fix a single missing argument.

---

## 6. Phase 5: The USB Gadget Dilemma (Why USB NCM Had to Die)

Once the serial console worked, we tried to enable USB Ethernet (CDC NCM) so the phone could share the host's internet connection.

Every time `s5-usb-ncm` was enabled, the USB serial console died within 2 to 9 minutes. 

### Why USB NCM and Serial Were Mutually Exclusive:
The Exynos composite USB gadget (`/sys/class/android_usb/android0`) implements functions dynamically. When the network script brought up `ncm0`, it toggled `android0/enable` off and on. 
* Toggling `android0/enable` tears down the entire USB composite gadget.
* Tearing down the gadget disconnects the CDC ACM serial line.
* When the serial line dropped, the inittab `getty` process received `SIGHUP` and terminated.

Because the serial console was our sole recovery lifeline if Wi-Fi ever failed, we made the architectural decision: **USB NCM was permanently removed from all runlevels.** The USB port would strictly serve as a rock-solid hardware ACM serial console. Networking would be handled 100% over Wi-Fi.

---

## 7. Phase 6: Standalone Headless Wi-Fi Orchestration

The Galaxy S5 uses a Broadcom BCM4354 dual-band 802.11ac Wi-Fi chip driven by `bcmdhd` over SDIO.

### The Wi-Fi Startup Sequence
Getting `wlan0` to initialize cleanly without NetworkManager (which pulled in 350+ heavy desktop dependencies) required a dedicated orchestration script (`s5-wifi`):
1. **Firmware Loading:** Ensure `/lib/firmware/postmarketos/bcmdhd_sta.bin` and factory-calibrated `nvram_net.txt` exist before the kernel interface initializes.
2. **RFKill Unblocking:** Explicitly unblock `rfkill unblock wifi`.
3. **WPA Supplicant Daemon:** Spawn `wpa_supplicant -B -i wlan0 -D nl80211 -c /etc/wpa_supplicant/wpa_supplicant.conf`.
4. **Self-Healing Loop:** Continuously monitor `wpa_cli status`. If link state drops from `COMPLETED`, trigger auto-reassociation.
5. **DHCP Acquisition:** Launch `udhcpc -b -i wlan0` with automated fallback if DHCP leases expire.

We packaged this as an OpenRC service (`s5-wifi` in runlevel `default`). On cold boot, the phone connects to the LAN within 18 seconds and acquires its local lease (`192.168.1.x`).

---

## 8. Phase 7: Hardware Power Safety & The "Store Mode" Breakthrough

One of the biggest hazards of using old smartphones as 24/7 Linux home servers is **battery swelling** and fire risk caused by holding a Li-ion cell at 100% capacity (4.35V) continuously under load.

### Investigating the MAX77804 PMIC Driver
We audited the kernel driver `/sys/class/power_supply/battery/` and `sec_battery.c`:
- Generic Linux `charging_enabled` nodes were read-only or unsupported.
- Generic voltage/current limiters were not exposed.

However, deep inside Samsung's proprietary battery management driver, we discovered:
```text
/sys/class/power_supply/battery/store_mode
```

### How Samsung Store Mode Works:
When `store_mode` is set to `1`:
- The MAX77804 PMIC hardware automatically **stops charging** when capacity reaches **70%**.
- It allows the device to run on battery until capacity drops to **60%**, at which point it resumes charging.
- The battery voltage never exceeds ~4.05V, drastically lowering cell stress and completely eliminating battery swelling during 24/7 continuous operation.

### The `s5-charge-limit` Daemon
Because the kernel resets `store_mode` to `0` upon cold boot, we built and deployed an OpenRC service `/etc/init.d/s5-charge-limit`:
* Activates `store_mode=1` during early boot.
* Runs a background watchdog that verifies `store_mode` remains active.
* Logs real-time battery voltage, temperature, and raw state-of-charge (SOC) to `/var/log/s5-battery.log`.

---

## 9. Phase 8: Building the Autonomous Edge AI Agent

With a stable Linux machine, rock-solid Wi-Fi, and safe 24/7 power, we transformed the phone into an **Autonomous AI Edge Node** managed via Telegram.

### The Stack:
* **Runtime:** Python 3.14 (in isolated virtual environment `/opt/s5-agent/venv`).
* **Framework:** Smolagents + custom async Telegram engine (`s5-agent.py` and `s5_agent_core.py`).
* **Supervisor:** OpenRC daemon `/etc/init.d/s5-agent`.

### 1. Local Hardware Tools
The agent is equipped with native tools that read the hardware directly:
* `get_battery_report()`: Reads live sysfs nodes (`capacity`, `batt_read_raw_soc`, `voltage_now`, `temp`, `store_mode`) to deliver real-time power diagnostics.
* `manage_wifi(action)`: Inspects signal strength, scans nearby SSIDs, and reports link status.
* `execute_bash(command)`: Safely executes local shell commands on the device with timeouts and output capture.
* `send_file_to_user(filepath)`: Sends files from the Galaxy S5 filesystem directly to the user's Telegram chat.

### 2. Instant Fast-Path Command Routing (<1s Latency)
Standard agent frameworks pass every incoming message to an LLM, consuming API credits and introducing 3–5 seconds of reasoning latency. We built a direct command dispatcher:
- Typing `/status`, `status`, `/id`, or `/ping` completely **bypasses the LLM**.
- The script queries the hardware sysfs nodes locally and replies in **under 1 second**, with zero API cost and zero latency.

### 3. Multi-Session Memory & Context Compaction
- Supports independent conversation sessions (`/sessions`, `/session <id>`, `/new`).
- Context auto-compactor: if a conversation exceeds the model's context window, older conversational turns are automatically summarized, preserving critical state while freeing tokens.

### 4. Bi-Directional File Transfers
- **Receiving:** Photos and documents sent to the bot in Telegram are downloaded into `/opt/s5-agent/downloads/` and staged for the agent to inspect.
- **Sending:** The `/get <filepath>` command allows the user to download logs, configs, or captured data directly from the phone.

---

## 10. Phase 9: Telegram Bot API vs. MTProto Protocol Investigation

When integrating the agent into a Telegram forum supergroup (`topics-watchman`), we encountered a fascinating distributed systems challenge: **Bot-to-Bot Communication**.

Another bot (`watchman` / `@BeastWatchmanBot`) was sending `@MeHomyBot /status` inside the forum topic `home` (`thread_id=4889`). `@MeHomyBot` never answered.

### The Investigation:
1. **Bot API Gateway:** We disabled Privacy Mode in `@BotFather` and verified `can_read_all_group_messages: true`. The message still never arrived.
2. **MTProto Bridge:** We deployed a binary MTProto client (`Telethon`) connecting directly as `@MeHomyBot` using `TELEGRAM_APP_API_ID` and `TELEGRAM_APP_API_HASH`.
3. **The Forensic Discovery:** In `/var/log/s5-mtproto.log`, we observed sequential message IDs arriving from the user: `4974`, `4976`, `4978`, `4981`. IDs `4975`, `4977`, `4979`, `4980` were missing.
4. When we queried Telegram's MTProto server directly for those missing message IDs:
```python
msgs = await client.get_messages(chat_id, ids=[4974, 4975, 4976, 4977, 4978, 4979, 4980, 4981])
# Telegram returned:
# [Message(4974), None, Message(4976), None, Message(4978), None, None, Message(4981)]
```
5. When querying `GetRepliesRequest` or `GetAdminLogRequest`, the server returned:
```text
BotMethodInvalidError: The API access for bot users is restricted. The method you tried to invoke cannot be executed as a bot.
```

### The Architectural Conclusion:
In Telegram's datacenter architecture, any session authenticated using a **bot token** is marked as `is_bot: true`. Telegram's servers **unconditionally drop messages originating from other bots** before delivering updates to any bot account, regardless of transport protocol (HTTP Bot API vs. binary MTProto) or admin permissions.

### The Resolution:
Rather than running an unauthorized userbot account, we streamlined the architecture:
- Removed the MTProto bridge and cleaned up credentials.
- Enforced strict authorization: `@MeHomyBot` **strictly answers only to the designated admin user ID** via explicit mention or direct reply.
- Naked commands and messages directed to `watchman` are 100% ignored, ensuring `@MeHomyBot` never interferes with other bots or conversations in the topic.

---

## 11. Key Lessons Learned & Hard Rules

1. **Busybox is not GNU Coreutils:** Always verify command-line argument syntax against Busybox applet documentation before writing inittab or initramfs scripts.
2. **Never sacrifice the Serial Console for USB Networking:** If USB network toggling tears down the gadget, eliminate USB networking and rely exclusively on Wi-Fi.
3. **Hardware Charge Limiting is Mandatory for Smartphone Servers:** Never leave a Li-ion battery at 100% capacity (4.35V) permanently. Always leverage hardware PMIC store modes (60–70% capacity) to prevent battery swelling.
4. **Device-Mapper is King for Nested Partitions:** When dealing with proprietary Android partition tables, avoid loopback auto-partitioning; use explicit device-mapper linear maps over sector offsets.
5. **Fast-Path Simple Commands:** Do not pass trivial system queries (status, uptime, battery) through an LLM. Route them directly to local sysfs readers for sub-second responses and zero API costs.

---

*This document was compiled from 400+ lines of engineering logs, 26 image builds, and live hardware telemetry on the Samsung Galaxy S5 (`SM-G900H`).*
