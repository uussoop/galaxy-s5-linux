#!/usr/bin/env python3
"""Nudge a live USB serial console that has already said its piece.

This exists because a getty that is sitting at its prompt produces no output. The
banner was printed once, to whoever happened to be listening at the time, and
the port is now silent because it is waiting to be spoken to. A capture program
that decides silence means dead will hang up on a perfectly healthy login, and
that is exactly what happened to the r26 capture: 120 s of nothing, then it left
a live console unclaimed.

So this sends a bare Enter, which is the one thing that is safe to send blind. It
cannot be a password: an empty line at a shell prompt is a no-op, and an empty
line at a login prompt asks for the login name. It cannot be a command, because
there is nothing to run it as until something has been typed.

Everything about the open is the recipe that has been proven to work on this
phone, and each part of it is there because the alternative failed at least once:
O_NONBLOCK so a silent port is not an error, the flag cleared around the write so
the write actually blocks until the phone takes it, setraw so the phone's line
discipline does not echo our own bytes back at us as though the phone said them,
and EIO and EAGAIN both treated as data-not-yet rather than as failure, because
on this device a draining USB endpoint reports exactly that.
"""
import argparse
import errno
import fcntl
import os
import select
import stat
import sys
import termios
import time
import tty

ap = argparse.ArgumentParser()
ap.add_argument("--node", help="the cu node; found from the descriptor if omitted")
ap.add_argument("--send", default="\r", help="what to send; a bare Enter by default")
ap.add_argument("--wait", type=float, default=25.0, help="seconds to listen for a reply")
ap.add_argument("--rounds", type=int, default=3, help="how many times to nudge and listen")
ap.add_argument("--raw", default="/tmp/s5-poke.raw", help="where to keep the bytes")
ap.add_argument("--show", action="store_true", help="print the bytes; they may hold a password")
a = ap.parse_args()


def find_node():
    import subprocess
    out = subprocess.run(["ioreg", "-p", "IOUSB", "-l", "-w", "0"],
                         capture_output=True, text=True).stdout
    serial = None
    for line in out.splitlines():
        if '"USB Serial"' in line and "01f44ecab714" in line:
            continue
        if '"id" = "01f44ecab714"' in line or "01f44ecab714" in line:
            serial = "01f44ecab714"
            break
    cands = sorted(
        n for n in os.listdir("/dev")
        if n.startswith("cu.usbmodem") and (serial is None or serial in n)
    )
    return "/dev/" + cands[-1] if cands else None


node = a.node or find_node()
if not node:
    sys.exit("no cu node found")
print(f"  node {node}")

fd = os.open(node, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
try:
    tty.setraw(fd)
    # Clear O_NONBLOCK for the writes, so a write blocks until the phone takes
    # it rather than raising EAGAIN the instant the endpoint is not ready.
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)

    raw = bytearray()
    for r in range(a.rounds):
        if r:
            time.sleep(2.0)
        payload = a.send.encode()
        try:
            os.write(fd, payload)
            print(f"  round {r+1}: sent {payload!r}")
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EIO):
                raise
            print(f"  round {r+1}: the port would not take the write ({e.strerror})")
            continue
        deadline = time.time() + a.wait
        got = 0
        while time.time() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.5)
            if not ready:
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EIO):
                    continue
                raise
            if not chunk:
                time.sleep(0.2)
                continue
            raw += chunk
            got += len(chunk)
            if not a.show:
                sys.stdout.write(".")
                sys.stdout.flush()
        if not a.show:
            print()
        print(f"  round {r+1}: {got} byte(s) back")
        if got:
            break
finally:
    os.close(fd)

# Create the file first, then chmod: chmod before the file exists raises.
with open(a.raw, "wb") as f:
    f.write(raw)
os.chmod(a.raw, stat.S_IRUSR | stat.S_IWUSR)
print(f"  {len(raw)} byte(s) into {a.raw}, mode 0600")

if a.show:
    sys.stdout.write(raw.decode("utf-8", "replace"))
    print()
else:
    print("  not shown; pass --show to print it. It is a console, not a secret store,")
    print("  but the password you type goes through here, so read it with care.")
