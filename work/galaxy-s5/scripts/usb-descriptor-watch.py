#!/usr/bin/env python3
"""Watch the host's view of the phone's USB descriptor, and log every change.

Why this exists. The r19 keeper re-asserts the acm function on android_usb
whenever it drifts, and writes what it found to CACHE. But CACHE is only
readable after a reboot into TWRP, which costs the user a physical key
combination and gives up whatever is on screen right now. This gives the same
signal passively, from the host, with no phone interaction at all: the phone's
usb gadget publishes bcdDevice in its descriptor, and on this kernel android.c
sets it to 0x0400 when acm is enabled and leaves it at 0xFFFF when it is not.

So the host can tell, at any moment and without asking the phone anything,
which of the two is currently on the wire:

    bcdDevice 1024   acm bound, USB serial console live
    bcdDevice 65535  acm gone, only the ncm network gadget

A single reading proves nothing. What matters is the trace over time: if 1024
holds for the length of a boot, the keeper is winning and the drift is gone. If
it flips back to 65535 at some point and then returns to 1024, the keeper is
fighting something and losing repeatedly, and the transition times here say when
to look for the keeper's own record of the same event.

Read-only. It only calls ioreg and lists /dev/cu.*. It never opens the serial
port, so it cannot touch a password the user is typing, and it never writes to
the phone.

    python3 scripts/usb-descriptor-watch.py --seconds 3600 --log out.txt
"""

import argparse
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

# android.c forces these bcdDevice values in enable_store. 0xFFFF is the "not
# changed" sentinel the gadget also advertises with ncm alone, which is what
# makes the two states distinguishable from the host.
ACM_BCD_DEVICE = 1024
NOT_ACM_BCD_DEVICE = 65535

VENDOR_GOOGLE = 6353  # 0x18D1
PRODUCT_SAMSUNG = 53249  # 0xD001

_FIELDS = (
    "idVendor",
    "idProduct",
    "bcdDevice",
    "bcdUSB",
    "USB Serial Number",
    "USB Product Name",
    "USB Vendor Name",
)


def now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def read_descriptor():
    """The phone's descriptor as the host currently sees it, or None.

    The phone is matched on vendor and product rather than on a device path,
    because the path changes on every re-enumeration and the point of this
    script is to notice re-enumerations.
    """
    try:
        out = subprocess.run(
            ["ioreg", "-p", "IOUSB", "-w", "0", "-l"],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    if f'"idVendor" = {VENDOR_GOOGLE}' not in out:
        return None
    if f'"idProduct" = {PRODUCT_SAMSUNG}' not in out:
        return None

    found = {}
    for line in out.splitlines():
        for field in _FIELDS:
            m = re.search(rf'"{re.escape(field)}" = "?([^"\n]*)"?', line)
            if m:
                found[field] = m.group(1).strip()
    if "bcdDevice" not in found:
        return None
    return found


def _acm_nodes_in(directory="/dev", serial=None):
    """acm_nodes against an arbitrary directory, so the match can be tested.

    The filtering is the thing under test and the real /dev is the only place
    it has ever been exercised, which is no way to test a filter. Everything
    else about the function is identical to acm_nodes; this only changes where
    it looks.
    """
    if not serial:
        return []
    try:
        return sorted(p.name for p in Path(directory).glob("cu.usbmodem*")
                      if serial in p.name)
    except OSError:
        return []


def acm_nodes(serial=None):
    """Serial nodes whose name carries the phone's own USB serial number.

    Matching on the serial rather than on "any usbmodem node" is deliberate:
    this Mac has other modems attached, and TWRP itself enumerates as
    usbmodem too, so only the node carrying the phone's descriptor serial is
    evidence about the real system. An earlier version of this function did not
    filter at all, which is why the r26 log lists cu.usbmodem11102 -- TWRP's
    node -- as though it were the phone's.

    The match is a substring, not a prefix or a suffix, and that is not a
    detail. macOS names the node cu.usbmodem<serial><n>, where the trailing n
    only exists to separate two identical devices, so the real name on this
    phone is cu.usbmodem01f44ecab7141 -- the serial is followed by a 1. A suffix
    match against the serial therefore never matches anything. An empty or
    absent serial means "no claim to make", and returns nothing rather than
    everything, because silently widening the filter is the exact bug this
    function previously had.
    """
    return _acm_nodes_in("/dev", serial)


def classify(desc):
    if desc is None:
        return "absent"
    try:
        bcd = int(desc.get("bcdDevice", "0"))
    except ValueError:
        return "unreadable"
    if bcd == ACM_BCD_DEVICE:
        return "acm"
    if bcd == NOT_ACM_BCD_DEVICE:
        return "ncm"
    return f"other({bcd})"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=int, default=3600)
    ap.add_argument("--log", default="/tmp/s5-usb-descriptor.log")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--serial", default="01f44ecab714",
                    help="the phone's USB serial; only nodes carrying it count")
    args = ap.parse_args()

    out = open(args.log, "a", buffering=1)
    started = time.monotonic()
    last = object()
    transitions = 0
    samples = 0

    def emit(text):
        out.write(f"[{now()}] {text}\n")
        print(text, flush=True)

    emit(f"watching {args.seconds}s, interval {args.interval}s, log {args.log}")
    emit("bcdDevice 1024 means acm (serial console live); 65535 means ncm only")

    while time.monotonic() - started < args.seconds:
        desc = read_descriptor()
        state = classify(desc)
        nodes = acm_nodes(args.serial)
        serial = desc.get("USB Serial Number", "?") if desc else "-"
        sample = (state, tuple(nodes))
        if sample != last:
            transitions += 1
            if state == "acm":
                verdict = "SERIAL CONSOLE LIVE"
            elif state == "ncm":
                verdict = "acm LOST, ncm only"
            elif state == "absent":
                verdict = "phone not on this bus"
            else:
                verdict = f"unexpected bcdDevice: {state}"
            emit(f"{verdict}  bcdDevice={desc.get('bcdDevice') if desc else '-'} "
                 f"serial={serial} nodes={nodes}")
            last = sample
        samples += 1
        time.sleep(args.interval)

    emit(f"done: {samples} samples, {transitions} state changes")


if __name__ == "__main__":
    main()
