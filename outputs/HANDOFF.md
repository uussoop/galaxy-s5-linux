# Handoff

Updated 2026-09-28. Project: `galaxy-s5-linux`.
Start with `README.md` — it carries the full journey. This file is the short
operational version: where things are and what to do next.

## Status

**Solved.** The phone boots native postmarketOS unattended, shows a login
prompt on the panel, gives a root shell over USB serial, rejoins Wi-Fi on its
own, and accepts key-only SSH. A full reboot test passed with every
configuration change intact.

There is no outstanding blocker. Do not re-run the installer, do not wipe
USERDATA, do not touch the external SD card, EFS, the modem, the bootloader or
TWRP.

## How to get in

**USB serial** — the primary channel and the one that must never be broken.

- Node: `/dev/cu.usbmodem01f44ecab7141` (the trailing `1` is a disambiguator).
- Log in as `root`. **The root password is an empty field** — press Enter.
- `python3 work/galaxy-s5/scripts/serial-drive.py --node <node> --commands 'uptime'`
  — budget 50–90 s per command.
- If the console is silent, the phone is probably fine. Send a real carriage
  return; the port may be live and echoing with no reader attached.

**SSH** — secondary, available whenever Wi-Fi is up at `<phone-ip>`.

- Key: `work/galaxy-s5/ssh/s5_agent_ed25519` or `~/.ssh/id_ed25519`. Passwordless sudo for `user`.
- Root cannot log in over SSH; that is intentional.
- `outputs/Test S5.command` runs a read-only check.
- Agent note: the user key can be passphrase-protected; automated tooling uses
  the dedicated repo agent key `work/galaxy-s5/ssh/s5_agent_ed25519`.

**TWRP** — the recovery path if the console is ever lost. Physical key combos
are the user's to press. It is toybox, not busybox; see `README.md`.

## Never restore these

- `/etc/inittab.s5bak` — deletes the working `ttyGS0` getty line. Next boot has
  no serial console and no other way in.
- `/etc/shadow.s5bak` — re-locks the console behind a password.

The getty line is the whole project in one line:

```
ttyGS0::respawn:/sbin/getty -L 115200 ttyGS0 vt100
```

`busybox getty` takes the baud rate *before* the tty name. Omitting `115200`
makes it parse `ttyGS0` as a speed and die.

## Do not re-enable USB NCM

`s5-usb-ncm` is stubbed out of the runlevel on purpose. Re-enabling it tears
down the USB gadget, and the gadget carries the console. Wi-Fi is the only
network path.

## Next work, in order of value

1. `passwd root` — the console is open to anyone holding the phone. The user
   types the passphrase; do not read the port while they do.
2. Remove `s5usbkeep` and `s5usbconsole` from `/etc/inittab`. `s5usbconsole`
   runs `/bin/sh -i` on a nonexistent tty and respawns forever. Check whether
   `s5usbkeep` is still load-bearing before deleting it.
3. r27 + reflash would clear three things at once: the CACHE logger and its two
   unremovable `(deleted)` mounts, the `s5-openrc-trace` lines that return every
   boot, and the phantom `KEY_VOLUMEUP` via a proper DT fix.

## Ground rules that were learned the hard way

- **Minimize reboots.** No keyboard, no working touch, no second way in. Gather
  all evidence before each one.
- **Never print or pull** the Wi-Fi profile, the SSH private host key, or any
  credential. No raw pulls of logs. On-screen status must be direct `/dev/fb0`
  pixel writes, fail-open only.
- **The on-screen login prompt is not a usable input path.** Do not suggest it.
- Physical key combos are the user's job. Phone operations are the root
  agent's job; no subagent touches the phone.
- Work is committed to git in this repo. It was untracked until 2026-09-28.
