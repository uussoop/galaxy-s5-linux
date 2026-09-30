#!/usr/bin/env python3
"""Watch the Mac for a USB gadget serial console, and log everything it says.

Written because the r14 boot already had a working /dev/ttyGS0 login and it went
unnoticed: during a native boot adb disappears, and adb was the only thing being
polled. This watches for the thing that actually appears instead.

Two details that matter and are easy to get wrong:

  * the callout node (/dev/cu.usbmodemNNNN) is the one to open. The dialin node
    (/dev/tty.usbmodemNNNN) blocks in open() until carrier detect is asserted,
    and a USB gadget serial port never asserts it, so opening the dialin node
    looks exactly like "the phone never showed up";
  * macOS reuses usbmodemNNNN numbers freely, so "a new node" is not reliable
    on its own. A node is only logged once, and a disappearance followed by a
    reappearance of the same name is treated as a fresh device.

adb transitions are logged too, purely as boot-phase markers: losing adb while
no serial node exists yet is the initramfs stage, and is what a halt looks like
from this side.

Usage: serial-watch.py [--seconds N] [--log FILE]
"""

import argparse
import os
import re
import subprocess
import time
from pathlib import Path

NODE = re.compile(r"^usbmodem\d+$")


def serial_nodes():
    """Callout nodes for USB serial devices, which is what a gadget exposes."""
    out = set()
    for p in Path("/dev").glob("cu.usbmodem*"):
        if NODE.match(p.name[len("cu.") :]):
            out.add(str(p))
    return out


def adb_state():
    """Which adb transports are up, as a sortable string for change detection."""
    try:
        r = subprocess.run(
            ["adb", "devices"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return "adb-unavailable"
    lines = [l.split()[0] for l in r.stdout.splitlines()[1:] if l.strip()]
    return ",".join(sorted(lines)) or "none"


def stamp(msg, t0, fh):
    print(f"[{time.time() - t0:7.1f}s] {msg}", file=fh, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=1800)
    ap.add_argument("--log", default="/tmp/s5-serial.log")
    args = ap.parse_args()

    t0 = time.time()
    known = serial_nodes()
    last_adb = adb_state()
    open_fds = {}

    with open(args.log, "w") as fh:
        stamp(f"baseline usbmodem nodes: {sorted(known) or 'none'}", t0, fh)
        stamp(f"baseline adb: {last_adb}", t0, fh)
        stamp(f"watching for {args.seconds}s, logging to {args.log}", t0, fh)

        while time.time() - t0 < args.seconds:
            now = serial_nodes()

            for node in sorted(now - known):
                stamp(f"SERIAL DEVICE APPEARED: {node}", t0, fh)
                try:
                    # Set a generous but finite timeout so a device that stops
                    # delivering bytes cannot wedge the reader forever.
                    import fcntl
                    import termios

                    fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
                    attrs = termios.tcgetattr(fd)
                    attrs[0] = 0  # iflag: no canonical processing, no echo
                    attrs[3] = 0  # lflag
                    termios.tcsetattr(fd, termios.TCSANOW, attrs)
                    fl = fcntl.fcntl(fd, fcntl.F_GETFL)
                    fcntl.fcntl(fd, fcntl.F_SETFL, fl)
                    open_fds[node] = fd
                    stamp(f"  opened {node}", t0, fh)
                except OSError as e:
                    stamp(f"  could not open {node}: {e}", t0, fh)

            for node in sorted(known - now):
                stamp(f"SERIAL DEVICE DISAPPEARED: {node}", t0, fh)
                if node in open_fds:
                    os.close(open_fds.pop(node))

            known = now

            for node, fd in list(open_fds.items()):
                try:
                    chunk = os.read(fd, 65536)
                except BlockingIOError:
                    continue
                except OSError as e:
                    stamp(f"  read from {node} failed: {e}", t0, fh)
                    open_fds.pop(node, None)
                    continue
                if not chunk:
                    continue
                text = chunk.decode("utf-8", "replace")
                for line in text.splitlines():
                    stamp(f"{node}: {line}", t0, fh)

            state = adb_state()
            if state != last_adb:
                stamp(f"adb: {last_adb} -> {state}", t0, fh)
                last_adb = state

            time.sleep(0.25)

        stamp("watch window closed", t0, fh)

    for fd in open_fds.values():
        os.close(fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
