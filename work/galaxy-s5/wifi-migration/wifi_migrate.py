#!/usr/bin/env python3
"""Move one Android Wi-Fi profile through TWRP RAM into native Linux."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import re
import secrets
import shlex
import subprocess
import sys
import time

SERIAL = "0000000000000000"
ADB = ("adb", "-s", SERIAL)
SOURCE = "/data/misc/wifi/wpa_supplicant.conf"
STAGE_DIR = "/tmp/codex-s5-wifi"
STAGE_FILE = STAGE_DIR + "/config"
STAGE_MARKER = STAGE_DIR + "/ready"
COMMIT_MARKER = STAGE_DIR + "/committed"
NATIVE_CONFIG = "/etc/wpa_supplicant/wpa_supplicant.conf"


class MigrationError(Exception):
    pass


def adb_capture(*args: str) -> bytes:
    try:
        result = subprocess.run((*ADB, *args), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MigrationError("ADB command unavailable or timed out") from None
    if result.returncode:
        raise MigrationError("ADB command failed; check the selected device and recovery state")
    return result.stdout


def adb_with_input(payload: bytes, command: str) -> None:
    try:
        # exec-in uses ADB's raw exec: service. Never send credentials to a PTY.
        result = subprocess.run((*ADB, "exec-in", "sh", "-c", command), input=payload,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise MigrationError("ADB transfer unavailable or timed out") from None
    if result.returncode:
        raise MigrationError("ADB transfer failed; no profile data was printed")


def strip_comment(line: str) -> str:
    quoted = False
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
        elif char == "\\" and quoted:
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char == "#" and not quoted:
            return line[:index].strip()
    return line.strip()


def parse_networks(raw: bytes) -> list[dict[str, str]]:
    # latin-1 keeps each original byte unchanged, including non-UTF8 SSIDs.
    lines = raw.decode("latin-1").splitlines()
    networks: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in lines:
        line = strip_comment(line)
        if not line:
            continue
        if current is None:
            if re.fullmatch(r"network\s*=\s*\{", line):
                current = {}
            continue
        if line == "}":
            networks.append(current)
            current = None
            continue
        match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]*)\s*=\s*(.*?)\s*", line)
        if not match:
            raise MigrationError("Unsupported multiline or malformed saved network")
        key, value = match.groups()
        if key in current:
            raise MigrationError("Duplicate saved network field")
        current[key] = value
    if current is not None:
        raise MigrationError("Incomplete saved network block")
    return networks


def parse_quoted_bytes(value: str) -> bytes:
    if len(value) < 2 or value[0] != '"' or value[-1] != '"':
        raise MigrationError("Unsupported quoted value")
    chars = value[1:-1]
    out = bytearray()
    index = 0
    while index < len(chars):
        char = chars[index]
        if char != "\\":
            out.append(ord(char))
            index += 1
            continue
        index += 1
        if index >= len(chars):
            raise MigrationError("Incomplete escape in saved network")
        char = chars[index]
        if char in {'\\', '"'}:
            out.append(ord(char))
            index += 1
        elif char in "nrt":
            out.append({"n": 10, "r": 13, "t": 9}[char])
            index += 1
        elif char == "x" and re.fullmatch(r"[0-9A-Fa-f]{2}", chars[index + 1:index + 3]):
            out.append(int(chars[index + 1:index + 3], 16))
            index += 3
        elif char in "01234567":
            match = re.match(r"[0-7]{1,3}", chars[index:])
            assert match is not None
            number = int(match.group(), 8)
            if number > 255:
                raise MigrationError("Invalid octal escape in saved network")
            out.append(number)
            index += len(match.group())
        else:
            raise MigrationError("Unsupported escape in saved network")
    return bytes(out)


def ssid_bytes(profile: dict[str, str]) -> bytes:
    value = profile.get("ssid", "")
    if value.startswith('"'):
        result = parse_quoted_bytes(value)
    elif re.fullmatch(r"[0-9A-Fa-f]{2,64}", value) and len(value) % 2 == 0:
        result = bytes.fromhex(value)
    else:
        raise MigrationError("Unsupported SSID encoding")
    if not 1 <= len(result) <= 32:
        raise MigrationError("SSID length is outside Wi-Fi limits")
    return result


def security_name(profile: dict[str, str]) -> str:
    keys = set(profile)
    management = set(profile.get("key_mgmt", "WPA-PSK").split())
    if any(key.startswith("wep_") for key in keys) or "NONE" in management:
        return "WEP/open (unsupported)"
    if any("EAP" in token or token == "IEEE8021X" for token in management) or \
            any(key in keys for key in ("identity", "password", "phase2", "eap")):
        return "Enterprise (unsupported)"
    if "SAE" in management or "OWE" in management:
        return "SAE/OWE (unsupported)"
    if management != {"WPA-PSK"} or "psk" not in keys:
        return "Other (unsupported)"
    return "WPA-PSK"


def display_ssid(profile: dict[str, str]) -> str:
    try:
        return safe_label(ssid_bytes(profile).decode("utf-8", "replace"))
    except MigrationError:
        return "<invalid SSID>"


def convert_profile(profile: dict[str, str]) -> bytes:
    if security_name(profile) != "WPA-PSK":
        raise MigrationError("Selected network security is unsupported")
    ssid = ssid_bytes(profile)
    value = profile["psk"]
    if value.startswith('"'):
        passphrase = parse_quoted_bytes(value)
        if not 8 <= len(passphrase) <= 63 or any(byte < 32 or byte > 126 for byte in passphrase):
            raise MigrationError("Unsupported passphrase encoding or length")
        quoted = passphrase.decode("ascii").replace("\\", "\\\\").replace('"', '\\"')
        psk = '"' + quoted + '"'
    elif re.fullmatch(r"[0-9A-Fa-f]{64}", value):
        psk = value.lower()
    else:
        raise MigrationError("Unsupported PSK encoding")
    if profile.get("disabled", "0") != "0":
        raise MigrationError("Selected network is disabled")
    lines = ["ctrl_interface=/run/wpa_supplicant", "update_config=0", "network={",
             "    ssid=" + ssid.hex(), "    psk=" + psk, "    key_mgmt=WPA-PSK"]
    if profile.get("scan_ssid", "0") == "1":
        lines.append("    scan_ssid=1")
    if profile.get("ieee80211w") in {"1", "2"}:
        lines.append("    ieee80211w=" + profile["ieee80211w"])
    lines.append("}")
    return ("\n".join(lines) + "\n").encode("ascii")


def mounted_filesystems() -> list[tuple[str, str, str]]:
    raw = adb_capture("exec-out", "cat", "/proc/mounts")
    mounts = []
    for line in raw.decode("utf-8", "replace").splitlines():
        fields = line.split()
        if len(fields) >= 3:
            mounts.append((fields[0], fields[1].replace("\\040", " "), fields[2]))
    return mounts


def check_ram_stage(mounts: list[tuple[str, str, str]]) -> None:
    candidates = [item for item in mounts if STAGE_DIR == item[1] or
                  STAGE_DIR.startswith(item[1].rstrip("/") + "/")]
    if not candidates:
        raise MigrationError("Cannot verify TWRP RAM staging filesystem")
    _source, _path, fstype = max(candidates, key=lambda item: len(item[1]))
    if fstype not in {"tmpfs", "ramfs", "rootfs"}:
        raise MigrationError("TWRP /tmp is not on a RAM filesystem")


def saved_profiles() -> list[dict[str, str]]:
    return parse_networks(adb_capture("exec-out", "cat", SOURCE))


def command_inspect(_args: argparse.Namespace) -> int:
    networks = saved_profiles()
    if not networks:
        raise MigrationError("No saved network blocks found")
    for index, profile in enumerate(networks):
        print(f"[{index}] SSID: {display_ssid(profile)} | Security: {security_name(profile)}")
    return 0


def command_stage(args: argparse.Namespace) -> int:
    networks = saved_profiles()
    if args.index is None:
        eligible = [index for index, profile in enumerate(networks)
                    if security_name(profile) == "WPA-PSK"]
        if len(eligible) != 1:
            raise MigrationError("Choose one saved WPA-PSK network with inspect and stage --index")
        index = eligible[0]
    else:
        index = args.index
    if index < 0 or index >= len(networks):
        raise MigrationError("Saved network index does not exist")
    profile = networks[index]
    payload = convert_profile(profile)
    check_ram_stage(mounted_filesystems())
    nonce = secrets.token_hex(16)
    command = ("umask 077; mkdir -p " + STAGE_DIR + " && chmod 700 " + STAGE_DIR +
               " && cat > " + STAGE_FILE + ".tmp && chmod 600 " + STAGE_FILE +
               ".tmp && mv -f " + STAGE_FILE + ".tmp " + STAGE_FILE +
               " && test -s " + STAGE_FILE +
               " && printf %s " + shlex.quote(nonce) + " > " + STAGE_MARKER)
    adb_with_input(payload, command)
    wait_for_marker(STAGE_MARKER, nonce)
    verify_ram_file(payload, STAGE_FILE)
    print(f"Staged and verified [{index}] {display_ssid(profile)} ({security_name(profile)}) in TWRP RAM")
    return 0


def verify_ram_file(expected: bytes, path: str) -> None:
    # exec-in does not carry remote exit status. Read back via raw exec-out.
    observed = adb_capture("exec-out", "cat", path)
    if not hmac.compare_digest(hashlib.sha256(expected).digest(),
                               hashlib.sha256(observed).digest()):
        raise MigrationError("RAM transfer did not match source")
    mode = adb_capture("exec-out", "stat", "-c", "%a", path).decode("ascii", "replace").strip()
    directory_mode = adb_capture("exec-out", "stat", "-c", "%a", STAGE_DIR).decode("ascii", "replace").strip()
    if mode != "600" or directory_mode != "700":
        raise MigrationError("RAM transfer permissions were not private")


def wait_for_marker(path: str, nonce: str, seconds: float = 60) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            marker = adb_capture("exec-out", "cat", path)
            if marker == nonce.encode("ascii"):
                return
        except MigrationError:
            pass
        time.sleep(0.25)
    raise MigrationError("Timed out waiting for remote RAM operation")


def wait_for_stage_removal(seconds: float = 60) -> None:
    deadline = time.monotonic() + seconds
    command = "if [ -e " + STAGE_FILE + " ]; then printf present; else printf absent; fi"
    while time.monotonic() < deadline:
        try:
            if adb_capture("exec-out", "sh", "-c", command) == b"absent":
                return
        except MigrationError:
            pass
        time.sleep(0.25)
    raise MigrationError("Installed profile verified but TWRP RAM stage remains")


def command_probe(_args: argparse.Namespace) -> int:
    check_ram_stage(mounted_filesystems())
    payload = b"codex-s5-raw-transport-probe\n"
    path = STAGE_DIR + "/probe"
    nonce = secrets.token_hex(16)
    command = ("umask 077; mkdir -p " + STAGE_DIR + " && chmod 700 " + STAGE_DIR +
               " && cat > " + path + " && chmod 600 " + path +
               " && printf %s " + shlex.quote(nonce) + " > " + STAGE_MARKER)
    adb_with_input(payload, command)
    wait_for_marker(STAGE_MARKER, nonce)
    verify_ram_file(payload, path)
    adb_capture("exec-out", "sh", "-c", "rm -f " + path + " " + STAGE_MARKER)
    print("Raw ADB stdin probe passed; RAM contents and permissions verified")
    return 0


def checked_mountpoint(path: str) -> str:
    if not path.startswith("/") or path in {"/", "/data", "/system", "/tmp", "/efs", "/cache"} \
            or ".." in path.split("/") or not re.fullmatch(r"/[A-Za-z0-9_./-]+", path):
        raise MigrationError("Unsafe native rootfs mountpoint")
    return path.rstrip("/")


def command_commit(args: argparse.Namespace) -> int:
    mountpoint = checked_mountpoint(args.mountpoint)
    matches = [item for item in mounted_filesystems() if item[1] == mountpoint]
    if len(matches) != 1 or matches[0][0] != args.expected_device or matches[0][2] not in {"ext4", "f2fs"}:
        raise MigrationError("Native rootfs mount/device verification failed")
    os_release = adb_capture("exec-out", "cat", mountpoint + "/etc/os-release")
    service = adb_capture("exec-out", "cat", mountpoint + "/etc/init.d/s5-wifi")
    if not re.search(rb'''(?m)^ID=(["']?)(?:postmarketos|alpine)\1\r?$''', os_release) or not service:
        raise MigrationError("Mounted filesystem lacks expected native Linux identity or Wi-Fi service")
    staged = adb_capture("exec-out", "cat", STAGE_FILE)
    if not staged:
        raise MigrationError("No RAM-staged Wi-Fi profile found")
    target = mountpoint + NATIVE_CONFIG
    directory = mountpoint + "/etc/wpa_supplicant"
    temp = target + ".codex-tmp"
    nonce = secrets.token_hex(16)
    command = ("set -eu; umask 077; test -s " + STAGE_FILE +
               "; test -f " + shlex.quote(mountpoint + "/etc/os-release") +
               "; test -f " + shlex.quote(mountpoint + "/etc/init.d/s5-wifi") +
               "; mkdir -p " + shlex.quote(directory) +
               "; chmod 700 " + shlex.quote(directory) +
               "; cp " + STAGE_FILE + " " + shlex.quote(temp) +
               "; chmod 600 " + shlex.quote(temp) +
               "; chown 0:0 " + shlex.quote(temp) +
               "; mv -f " + shlex.quote(temp) + " " + shlex.quote(target) +
               "; sync; printf %s " + shlex.quote(nonce) + " > " + COMMIT_MARKER)
    adb_with_input(b"", command)
    wait_for_marker(COMMIT_MARKER, nonce)
    installed = adb_capture("exec-out", "cat", target)
    if not hmac.compare_digest(hashlib.sha256(staged).digest(),
                               hashlib.sha256(installed).digest()):
        raise MigrationError("Installed Wi-Fi profile did not match RAM stage")
    ownership = adb_capture("exec-out", "stat", "-c", "%a:%u:%g", target).decode("ascii", "replace").strip()
    if ownership != "600:0:0":
        raise MigrationError("Installed Wi-Fi profile permissions were not root-only")
    adb_capture("exec-out", "sh", "-c", "rm -f " + STAGE_FILE)
    wait_for_stage_removal()
    print(f"Installed one Wi-Fi profile at {NATIVE_CONFIG} (root:root, mode 0600)")
    return 0


def current_ssid_from_dump(raw: bytes) -> str | None:
    # Dumpsys may include saved configurations. Only inspect WifiInfo lines.
    dump = raw.decode("utf-8", "replace")
    for line in dump.splitlines():
        if not re.search(r"\b(?:mWifiInfo|WifiInfo)\b", line):
            continue
        match = re.search(r"\bSSID:\s*(\"(?:\\.|[^\"\\])*\"|[^,\s]+)", line)
        if not match:
            continue
        candidate = match.group(1)
        if candidate.lower() in {"<unknown", "<unknown ssid>", "unknown", "none"}:
            continue
        if candidate.startswith('"') and candidate.endswith('"'):
            candidate = candidate[1:-1]
        if candidate:
            return candidate
    return None


def safe_label(label: str) -> str:
    return "".join(ch if ch.isprintable() and ch not in "\x1b\r\n" else "?" for ch in label)


def command_current(_args: argparse.Namespace) -> int:
    raw = adb_capture("exec-out", "dumpsys", "wifi")
    ssid = current_ssid_from_dump(raw)
    if ssid is None:
        print("Current Wi-Fi SSID: unavailable")
        return 2
    print(f"Current Wi-Fi SSID: {safe_label(ssid)}")
    return 0


def command_diagnose(_args: argparse.Namespace) -> int:
    raw = adb_capture("exec-out", "dumpsys", "wifi")
    dump = raw.decode("utf-8", "replace")
    lines = dump.splitlines()
    info_lines = [line for line in lines if re.search(r"\b(?:mWifiInfo|WifiInfo)\b", line)]
    network_lines = [line for line in lines if re.search(r"\b(?:mNetworkInfo|NetworkInfo)\b", line)]
    print(f"Wi-Fi dump available: {bool(raw)}")
    print(f"WifiInfo lines: {len(info_lines)}")
    print(f"WifiInfo SSID colon fields: {sum('SSID:' in line for line in info_lines)}")
    print(f"WifiInfo SSID equals fields: {sum('SSID=' in line for line in info_lines)}")
    print(f"NetworkInfo lines: {len(network_lines)}")
    print(f"NetworkInfo connected markers: {sum('CONNECTED' in line for line in network_lines)}")
    print(f"Wi-Fi enabled markers: {bool(re.search(r'Wi-Fi is enabled|mWifiEnabled\s*[=:]\s*true|mWifiState\s*[=:]\s*(?:enabled|3)', dump, re.I))}")
    for index, line in enumerate(info_lines[:4]):
        match = re.search(r"\bSSID:\s*(\"(?:\\.|[^\"\\])*\"|.*?)(?:,\s*(?:BSSID|MAC|Supplicant|networkId|RSSI)\b|$)", line)
        candidate = match.group(1).strip() if match else "unrecognized"
        print(f"WifiInfo SSID candidate {index}: {safe_label(candidate[:80])}")
    for index, line in enumerate(network_lines[:2]):
        match = re.search(r"\bstate:\s*([A-Z_]+)", line)
        state = match.group(1) if match else "unrecognized"
        print(f"NetworkInfo state {index}: {state}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    subparsers.add_parser("current", help="print only the currently connected SSID from stock Android")
    subparsers.add_parser("diagnose", help="print only Wi-Fi status and dump field-shape metadata")
    subparsers.add_parser("inspect", help="list saved SSIDs and security types from TWRP")
    subparsers.add_parser("probe", help="test raw ADB stdin with nonsecret bytes")
    stage = subparsers.add_parser("stage", help="send one WPA-PSK profile into TWRP RAM")
    stage.add_argument("--index", type=int, help="index shown by inspect; auto-selects if unique")
    commit = subparsers.add_parser("commit", help="copy RAM-staged profile into mounted native rootfs")
    commit.add_argument("--mountpoint", required=True, help="verified TWRP mount path of native rootfs")
    commit.add_argument("--expected-device", required=True, help="exact block device in /proc/mounts")
    args = parser.parse_args()
    try:
        if args.mode == "current":
            return command_current(args)
        if args.mode == "diagnose":
            return command_diagnose(args)
        if args.mode == "inspect":
            return command_inspect(args)
        if args.mode == "probe":
            return command_probe(args)
        if args.mode == "stage":
            return command_stage(args)
        if args.mode == "commit":
            return command_commit(args)
    except MigrationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
