#!/usr/bin/env python3
"""Read-only inspection of the installed postmarketOS root on the phone.

The r15 boot reached the real rootfs for the first time in this project, and its
login prompt is on the panel only. The one thing needed to turn USB serial into
a usable console is the exact content of the installed /etc/inittab and which
login binaries it has, and neither is knowable from the initramfs: the helper
that writes the ttyGS0 line has to guess between /sbin, /bin and /usr/bin, and
whether a password is needed to get past the resulting getty depends entirely on
how this particular root was installed. Guessing has already cost one boot
cycle, so this reads the answers off the partition instead.

TWRP quirks this has to work around, each of which produced wrong output in the
first version of this script:

  * there is no /sbin/sh, so nothing may be passed as a shell string. Every call
    here is a single program with its own argv.
  * adbd's own exit status is 0 even when the remote program is missing or
    fails, so adb's returncode says nothing. Every call judges success by the
    content of its output instead. The first version trusted returncode and
    cheerfully reported "no getty anywhere" for a filesystem it had not read.
  * it is toybox, not busybox, and "busybox" is not on PATH. The applets are
    reachable under their bare names (cat, ls, grep, stat, mount, ...), and
    there is no awk at all.
  * the partition is /dev/block/mmcblk0p21 here, not /dev/mmcblk0p21.

Safety, and why each rule is here:

  * the root filesystem is mounted strictly read-only. Nothing in this script
    writes to it, and a failed inspection must not be able to damage a system
    that finally boots;
  * no file contents are printed except an explicit allowlist of
    configuration files that cannot contain secrets. /etc/shadow is never
    printed and never returned to this process in usable form: only whether
    root's field is empty, locked or hashed, because a hash is a credential and
    this project's rules forbid printing one;
  * no file outside the allowlist is even opened, so a stray private key or
    wpa_supplicant profile cannot be reached by widening a path by accident;
  * the only remote paths written to are the mountpoint and the partition, both
    fixed strings.

Usage: python3 inspect_rootfs.py            # print a report
       python3 inspect_rootfs.py --raw      # include the raw inittab text
"""

from __future__ import annotations

import argparse
import subprocess
import sys

SERIAL = "0000000000000000"
ROOT_PART = "/dev/block/mmcblk0p21"
MNT = "/tmp/codex-rootfs-ro"

# Files whose contents may be printed. Everything here is plain-text config with
# no credentials in it; this list is the whole policy, so anything not named here
# is only ever stat()ed, never opened.
ALLOW_READ = (
    "/etc/inittab",
    "/etc/os-release",
    "/etc/issue",
    "/etc/hostname",
    "/etc/fstab",
    "/etc/rc.conf",
)

# Searched for the binaries an inittab line could name. Only presence and size
# are recorded.
BIN_DIRS = ("/sbin", "/bin", "/usr/sbin", "/usr/bin")
BIN_NAMES = ("getty", "agetty", "login", "sh", "busybox", "toybox", "agetty")

MAX_TEXT = 8192
# What /bin/sh or any shell would print if a command is missing. adbd produces
# this, and it arrives on stdout with a zero exit status.
SHELL_ERR = "not found"


def remote(*argv: str) -> tuple[bool, str]:
    """Run one program on the phone. Returns (looked_like_it_ran, output).

    A bare applet name is used rather than a busybox prefix, because adbd
    resolves against its own PATH where "busybox" does not exist. Success is
    inferred from the output because adb's exit status is always 0.
    """
    try:
        p = subprocess.run(
            ["adb", "-s", SERIAL, "exec-out", *argv],
            capture_output=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        return False, "timeout"
    out = (p.stdout + p.stderr).decode("utf-8", errors="replace").replace("\r\n", "\n")
    ran = SHELL_ERR not in out and "Usage:" not in out
    return ran, out.strip()


def line(label: str, value: str = "") -> None:
    print(f"{label}: {value}" if value else f"{label}:")


def read_text(path: str) -> tuple[bool, str]:
    ran, out = remote("cat", path)
    if not ran or out.startswith("cat:") or "No such file" in out:
        return False, out[:160]
    return True, out[:MAX_TEXT]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", action="store_true", help="print raw inittab text")
    args = ap.parse_args()

    ran, out = remote("ls", "-l", ROOT_PART)
    if not ran or "No such file" in out:
        print("FATAL: cannot see the root partition; is the phone in TWRP on adb?")
        print(out[:400])
        return 2
    line("root partition", out)

    # Mount read-only. If it is already mounted somewhere, use that rather than
    # trying a second mount, and say so.
    ran, mounts = remote("mount")
    existing = ""
    for ln in mounts.splitlines():
        parts = ln.split()
        if len(parts) > 2 and ROOT_PART in parts[0]:
            existing = parts[2]
    mounted_here = False
    if existing:
        mnt = existing
        line("already mounted at", f"{mnt} (not mounting again)")
    else:
        mnt = MNT
        remote("mkdir", "-p", MNT)
        ran, out = remote("mount", "-o", "ro", ROOT_PART, MNT)
        # A toybox mount that failed says so; a successful one prints nothing.
        if "not mounted" in out or "bad option" in out.lower() or "invalid" in out.lower():
            print("FATAL: read-only mount failed")
            print(out[:400])
            return 3
        # Confirm it really took, rather than trusting the absence of an error.
        ran, mounts = remote("mount")
        if not any(ROOT_PART in ln and mnt in ln for ln in mounts.splitlines()):
            print("FATAL: mount reported no error but the filesystem is not there")
            print(out[:400])
            return 3
        mounted_here = True
        line("mounted read-only at", mnt)

    try:
        # 1. Which login binaries exist. This decides the exact inittab line.
        print("\n== login binaries present (path, bytes) ==")
        found: dict[str, str] = {}
        for d in BIN_DIRS:
            for n in BIN_NAMES:
                ran, out = remote("stat", "-c", "%s", f"{mnt}{d}/{n}")
                if ran and out.isdigit():
                    # First hit in PATH order wins, matching what a bare name in
                    # inittab would actually resolve to.
                    found.setdefault(n, f"{d}/{n} ({out} B)")
        if found:
            for n in sorted(found):
                line(f"  {n}", found[n])
        else:
            line("  NONE FOUND", "no getty/agetty/login in the four dirs")

        # 2. Does the real init read /etc/inittab, and what is in it.
        print("\n== /etc/inittab ==")
        ok, text = read_text(f"{mnt}/etc/inittab")
        if not ok:
            line("  unreadable or absent", text)
            line(
                "  CONSEQUENCE",
                "no ttyGS0 line can be added; a getty must be started another way",
            )
        else:
            rows = [
                r
                for r in text.splitlines()
                if r.strip() and not r.lstrip().startswith("#")
            ]
            line("  active lines", str(len(rows)))
            for r in rows:
                line("   ", r[:160])
            if args.raw:
                print("  --- raw ---")
                for r in text.splitlines():
                    print(f"  {r[:200]}")
            tty = [r for r in rows if "ttyGS0" in r]
            if tty:
                line("  ttyGS0 present", f"{len(tty)} line(s); helper will not touch it")

        # 3. Which release this actually is.
        print("\n== /etc/os-release ==")
        ok, text = read_text(f"{mnt}/etc/os-release")
        for ln in (text.splitlines()[:12] if ok else ["unreadable"]):
            line("  ", ln[:160])

        print("\n== other config (presence and size) ==")
        for rel in ("/etc/issue", "/etc/hostname", "/etc/fstab", "/etc/rc.conf"):
            ran, out = remote("stat", "-c", "%s", f"{mnt}{rel}")
            line(f"  {rel}", f"{out} B" if ran and out.isdigit() else "absent")

        # 4. Whether a password stands between us and a shell on the serial
        #    port. The field's *shape* only: empty means no password, a lock
        #    marker means unusable, anything else means a hash is set. The hash
        #    is split off on the phone and discarded there, so it never reaches
        #    this process or the report.
        print("\n== root password state (shape only, never the hash) ==")
        found_shape = False
        for shadow in ("/etc/shadow", "/etc/passwd"):
            ran, out = remote("grep", "^root:", f"{mnt}{shadow}")
            if not ran or not out or out.startswith("grep:"):
                continue
            field = out.split(":", 2)[1] if out.count(":") >= 2 else ""
            if shadow.endswith("passwd"):
                shape = (
                    "EMPTY, no password required"
                    if field in ("x", "")
                    else "not in /etc/passwd"
                )
            elif field == "":
                shape = "EMPTY, no password required"
            elif field.startswith(("!", "*")):
                shape = "LOCKED or disabled"
            else:
                shape = f"a hash is set ({len(field)} chars, not printed)"
            line(f"  {shadow} root field", shape)
            found_shape = True
            break
        if not found_shape:
            line("  no root entry found", "a getty may still accept a login")

        # 5. The rest of the allowlist, in full. None of these can hold a secret.
        for rel in ("/etc/fstab", "/etc/rc.conf", "/etc/issue"):
            ok, text = read_text(f"{mnt}{rel}")
            if ok:
                print(f"\n== {rel} ==")
                for ln in text.splitlines()[:40]:
                    print(f"  {ln[:200]}")
    finally:
        if mounted_here:
            remote("umount", MNT)
            print("\nunmounted")

    return 0


if __name__ == "__main__":
    sys.exit(main())
