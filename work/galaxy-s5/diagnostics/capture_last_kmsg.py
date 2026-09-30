#!/usr/bin/env python3
"""Capture a redacted S5 boot log from the explicitly selected device."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

SERIAL = "0000000000000000"
HERE = Path(__file__).resolve().parent
MAX_BYTES = 16 * 1024 * 1024
# The previous rule also matched *any* "word=value", which redacted ~20% of a
# normal kernel log -- including "usb: enable_store vendor=18d1,product=d001",
# which is exactly the evidence this tool exists to collect. Redaction is now
# driven by the *name* on the left of "=" or ":" instead of by the mere
# presence of an assignment. A credential word anywhere in the line still
# redacts the whole line, so this is narrower in what it hides only where the
# old rule was hiding non-secrets.
#
# A value is treated as a secret when any component of the name on the left of
# "=" or ":" is one of these. Components are split on "_", "-", "." and
# camelCase, so "api_secret", "WPA-PSK", "wpaPsk" and "rootPassword" are all
# caught while "monkey" and "vendor" are not.
#
# "keys" is deliberately absent. This kernel prints "keys:flip_cover_work" on
# every boot, so treating a bare "keys" as a credential would redact a real line
# on every single capture. The plural is handled separately below, and only for
# names that have more than one component ("api_keys" yes, "keys" no).
CRED_COMPONENTS = frozenset(
    (
        "auth", "bssid", "credential", "credentials", "key", "login",
        "pass", "passphrase", "passwd", "password", "pin", "pkk", "pmk", "ppk",
        "psk", "pwd", "secret", "secrets", "ssid", "token", "user", "username",
        "wep",
    )
)

CRED_PLURALS = ("keys", "tokens", "secrets", "credentials", "passwords")

_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")
_SEPARATORS = re.compile(r"[^A-Za-z0-9]+")


def name_components(name: str) -> set[str]:
    """Split an assignment name into lowercase words, honouring camelCase."""
    words: set[str] = set()
    for chunk in _SEPARATORS.split(name):
        if not chunk:
            continue
        words.add(chunk.lower())
        words.update(part.lower() for part in _CAMEL.findall(chunk))
    return words

# A bare credential word still redacts the entire line.
CRED_WORD = re.compile(
    r"password|passphrase|\bpsk\b|\bpmk\b|\bptk\b|wpa[ _-]?key|"
    r"\bssid\b|wpa_supplicant|\bsecret\b|\bcredential\b|\btoken\b|\bkey\b",
    re.IGNORECASE,
)

ASSIGNMENT = re.compile(
    r"(?P<name>[A-Za-z_][A-Za-z0-9_.-]*)(?P<sep>\s*[=:]\s*)"
    r"(?P<quote>[\"']?)(?P<value>[^\s\"']+)"
)

# Key/hash shaped material, redacted whatever it is called.
OPAQUE = re.compile(r"\A(?:[0-9a-fA-F]{32,}|[A-Za-z0-9+/=_-]{40,})\Z")


def mask_values(line: str) -> str:
    """Redact only the value of assignments that name a secret."""

    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        value = match.group("value")
        parts = name_components(name)
        plural = len(parts) > 1 and name.lower().endswith(CRED_PLURALS)
        if parts & CRED_COMPONENTS or plural or OPAQUE.match(value):
            quote = match.group("quote")
            return f"{name}{match.group('sep')}{quote}[redacted]{quote}"
        return match.group(0)

    return ASSIGNMENT.sub(replace, line)


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
FATAL = re.compile(
    r"kernel panic|unable to handle|internal error|\boops\b|"
    r"fatal exception|watchdog|call trace|\bBUG:", re.IGNORECASE
)
VALID_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.txt\Z")
REMOTE_ALLOWLIST = (
    re.compile(
        r"/cache/codex-s5-diagnostics/boot-[A-Za-z0-9]{8}/"
        # capped is the keeper's own zero-byte marker for "usb.log hit its size
        # cap and stopped recording". It has no content, but its presence is the
        # only way to tell a log that ended because the boot ended from one that
        # ended because the logger ran out of room, so it is worth collecting.
        r"(?:kernel[.]log|initramfs[.]log|usb[.]log|stage|capped)\Z"
    ),
    re.compile(
        r"/tmp/codex-s5-native/var/log/s5-boot-diagnostics/"
        r"(?:sysinit|boot|default)[.]log\Z"
    ),
    re.compile(r"/tmp/codex-s5-native/var/log/(?:dmesg|rc[.]log)\Z"),
)


def allowed_remote(path: str) -> bool:
    return any(pattern.fullmatch(path) for pattern in REMOTE_ALLOWLIST)


def sanitize(raw: bytes) -> tuple[str, list[str], int]:
    lines = raw.decode("utf-8", errors="replace").splitlines()
    cleaned: list[str] = []
    selected: list[str] = []
    redacted = 0
    for line in lines:
        if CRED_WORD.search(line):
            cleaned.append("[redacted sensitive line]")
            redacted += 1
            continue
        line = ANSI_ESCAPE.sub("", line)
        line = mask_values(line)
        safe = "".join(char if char.isprintable() or char == "\t" else "?" for char in line)
        cleaned.append(safe)
        if FATAL.search(safe) and len(selected) < 8:
            selected.append(safe[:220])
    return "\n".join(cleaned) + "\n", selected, redacted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", help="new diagnostics filename, for example third-native-last-kmsg.txt")
    parser.add_argument("--remote", help="allowlisted persistent boot log; defaults to /proc/last_kmsg")
    args = parser.parse_args()
    if not VALID_NAME.fullmatch(args.name) or ".." in args.name:
        print("Error: use a simple new .txt filename", file=sys.stderr)
        return 2
    if args.remote is not None and not allowed_remote(args.remote):
        print("Error: remote source is not on the boot-log allowlist", file=sys.stderr)
        return 2
    remote = args.remote if args.remote is not None else "/proc/last_kmsg"
    destination = HERE / args.name
    if destination.exists():
        print("Error: destination exists; choose a new filename", file=sys.stderr)
        return 1

    try:
        result = subprocess.run(
            ("adb", "-s", SERIAL, "exec-out", "cat", remote),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        print("Error: ADB capture unavailable or timed out", file=sys.stderr)
        return 1
    if result.returncode != 0:
        print("Error: ADB capture failed for the selected device", file=sys.stderr)
        return 1
    if not result.stdout or len(result.stdout) > MAX_BYTES:
        print("Error: boot log is empty or exceeds the capture limit", file=sys.stderr)
        return 1

    safe_text, selected, redacted = sanitize(result.stdout)
    try:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print("Error: destination exists; choose a new filename", file=sys.stderr)
        return 1
    except OSError:
        print("Error: could not create diagnostics file", file=sys.stderr)
        return 1
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            output.write(safe_text)
    except OSError:
        print("Error: could not complete diagnostics write", file=sys.stderr)
        return 1

    print(f"Saved {len(safe_text.splitlines())} sanitized lines ({redacted} redacted) to {destination}")
    for line in selected:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
