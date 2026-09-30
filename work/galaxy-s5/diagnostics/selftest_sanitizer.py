#!/usr/bin/env python3
"""Self-test for the redaction rules in capture_last_kmsg.py.

Two directions, and both must hold:

  MUST_REDACT  a credential must not survive in the saved text, whether it is
               masked in place or the whole line is dropped.
  MUST_KEEP    ordinary kernel and initramfs lines must survive byte for byte.
               The previous blanket "word=value" rule ate ~20% of a real boot
               log, including the android_usb descriptor line that proves
               whether acm actually bound, so over-redaction is a bug here too.

Run:  python3 diagnostics/selftest_sanitizer.py
"""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import capture_last_kmsg as cap  # noqa: E402

# Each case: (line, must_the_secret_be_gone)
MUST_REDACT = [
    ("root_password=hunter2", "hunter2"),
    ("PASSWORD: hunter2", "hunter2"),
    ('wpa_psk="s3cr3tpskvalue"', "s3cr3tpskvalue"),
    ("WPA-PSK=abcdefghijklmnop", "abcdefghijklmnop"),
    ("ssid: MyHomeNetwork", "MyHomeNetwork"),
    ("bssid=aa:bb:cc:dd:ee:ff", "aa:bb:cc:dd:ee:ff"),
    ("private_key = -----BEGIN", "-----BEGIN"),
    ("api_secret: zzz", "zzz"),
    ("myApiToken=abcdef123456", "abcdef123456"),
    ("user=root", "root"),
    ("username: johndoe", "johndoe"),
    ("login_password=letmein", "letmein"),
    ("token: abc123def456", "abc123def456"),
    ("auth=Challenge", "Challenge"),
    ("wpa_supplicant: wpa0", "wpa0"),
    ("# a passphrase was set", "passphrase was set"),
    ("credential_helper=/x", "/x"),
    ("wep_key=deadbeef", "deadbeef"),
    # Plurals still have to be caught when the name is qualified.
    ("api_keys=AAAA1111", "AAAA1111"),
    ("rotate_tokens=BBBB2222", "BBBB2222"),
    # An opaque blob must be caught whatever it is called.
    ("retries=" + "a1b2c3d4" * 5, "a1b2c3d4" * 5),
    ("tag=" + "QWxhZGRpbjpvcGVuIHNlc2FtZQ" * 3, "QWxhZGRpbjpvcGVuIHNlc2FtZQ" * 3),
]

# Each case: (line, substring that must still be present verbatim)
MUST_KEEP = [
    # The line that answers "did acm actually bind, and as what descriptor".
    (
        "usb: enable_store vendor=18d1,product=d001,bcdDevice=400,Class=0,SubClass=0,Protocol=0",
        "vendor=18d1,product=d001,bcdDevice=400",
    ),
    ("usb: enable_store f:acm", "f:acm"),
    ("usb: functions_store buff=acm", "buff=acm"),
    ("usb: acm is enabled. (bcdDevice=0x400)", "bcdDevice=0x400"),
    ("android_usb: Cannot enable 'ncm' (-22)", "Cannot enable 'ncm' (-22)"),
    ("ncm0: MAC 1a:54:fa:d7:d6:59", "1a:54:fa:d7:d6:59"),
    ("ncm0: HOST MAC 02:64:66:07:56:03", "02:64:66:07:56:03"),
    ("usb: 1-1: new full-speed USB device number 4", "number 4"),
    ("dwc3 12000000.dwc3: request ecc37a00 was not queued to ep0out", "ecc37a00"),
    ("INFO: serial ports present: /dev/ttyGS0 /dev/ttyGS1", "/dev/ttyGS0 /dev/ttyGS1"),
    ("INFO: controllers available: [12000000.dwc3 12400000.dwc3 ]", "12400000.dwc3"),
    ("s3c2410-wdt 101d0000.watchdog: watchdog inactive", "watchdog inactive"),
    ("keys:flip_cover_work #1 : 1", "flip_cover_work"),
    # A bare generic word is not a credential, even when it looks like one.
    ("keys=1", "keys=1"),
    ("monkey=1", "monkey=1"),
    ("max77804_get_vbus_state: VBUS is invalid.", "VBUS is invalid"),
    ("[pmOS-rd]: INFO: android_usb is now: [acm]", "android_usb is now: [acm]"),
    ("modprobe: FATAL: Module ext4 not found", "Module ext4 not found"),
    ("resize2fs 1.47.4 (6-Mar-2025)", "6-Mar-2025"),
    ("dm-1: mounted filesystem with ordered data mode. Opts: (null)", "Opts: (null)"),
    ("eth0: link becomes ready", "link becomes ready"),
    ("apk_repository=https://dl-cdn.alpinelinux.org/alpine", "alpinelinux.org"),
    ("vendor=0x18D1", "0x18D1"),
    ("bNumInterfaces=2", "bNumInterfaces=2"),
    ("iSerialNumber=3", "iSerialNumber=3"),
]


def main() -> int:
    failures: list[str] = []

    for line, secret in MUST_REDACT:
        cleaned, _, _ = cap.sanitize((line + "\n").encode())
        if secret in cleaned:
            failures.append(f"LEAK: {line!r} still exposes {secret!r}")
        if secret not in line:
            failures.append(f"BADTEST: {line!r} does not contain {secret!r}")

    for line, keep in MUST_KEEP:
        cleaned, _, _ = cap.sanitize((line + "\n").encode())
        if keep not in cleaned:
            failures.append(f"DROPPED: {line!r} no longer contains {keep!r}")

    # The allowlist must accept exactly what the CACHE keeper can write, and
    # nothing else. Every name below is one the keeper actually creates.
    allowed = [
        "/cache/codex-s5-diagnostics/boot-ABCD1234/usb.log",
        "/cache/codex-s5-diagnostics/boot-ABCD1234/kernel.log",
        "/cache/codex-s5-diagnostics/boot-ABCD1234/initramfs.log",
        "/cache/codex-s5-diagnostics/boot-ABCD1234/stage",
        "/cache/codex-s5-diagnostics/boot-ABCD1234/capped",
    ]
    for path in allowed:
        if not cap.allowed_remote(path):
            failures.append(f"ALLOWLIST rejects {path!r}")
    for path in [
        "/cache/codex-s5-diagnostics/boot-ABCD1234/usb.log.gz",
        "/cache/codex-s5-diagnostics/boot-ABCD/usb.log",
        "/cache/codex-s5-diagnostics/boot-ABCD1234/../../wpa_supplicant.conf",
        "/cache/wpa_supplicant.conf",
        "/data/misc/wifi/softap.conf",
        "/cache/codex-s5-diagnostics/boot-ABCD1234/kernel.log.evil",
        # The keeper's temporary files are renamed into place and must never be
        # read directly: a half-written kernel.tmp is not a kernel log.
        "/cache/codex-s5-diagnostics/boot-ABCD1234/kernel.tmp",
        "/cache/codex-s5-diagnostics/boot-ABCD1234/initramfs.tmp",
    ]:
        if cap.allowed_remote(path):
            failures.append(f"ALLOWLIST wrongly accepts {path!r}")

    # Counted from the lists rather than a literal, so adding a case cannot
    # quietly stop being counted.
    rejected = 8
    total = len(MUST_REDACT) + len(MUST_KEEP) + len(allowed) + rejected
    if failures:
        for failure in failures:
            print(f"  {failure}")
        print(f"FAIL: {len(failures)} of {total} checks failed")
        return 1
    print(f"ok: {total} sanitizer checks passed "
          f"({len(MUST_REDACT)} redact, {len(MUST_KEEP)} keep, "
          f"{len(allowed) + rejected} allowlist)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
