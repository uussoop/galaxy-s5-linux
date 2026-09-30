#!/usr/bin/env python3
"""Drive the live USB serial console: log in, then run commands and read back.

Built for the case where the console is already up and has already printed its
banner, so nothing that waits for a banner will work. It logs in as root with an
empty password, which is what r25 arranged by blanking the root field in
/etc/shadow and keeping the original beside it, and then runs whatever commands
are given and prints what comes back.

The password is never sent because there is not one to send. If the phone asks
for a password anyway, that means the blanking did not land, and this stops and
says so rather than typing something into a password field: a wrong guess costs
the window, and a correct guess typed by accident into the wrong field is worse.
"""
import argparse
import errno
import fcntl
import os
import re
import select
import stat
import sys
import time
import tty

ap = argparse.ArgumentParser()
ap.add_argument("--node", required=True)
ap.add_argument("--commands", nargs="*", default=[], help="shell commands to run, in order")
ap.add_argument("--login-wait", type=float, default=20.0)
ap.add_argument("--cmd-wait", type=float, default=25.0)
ap.add_argument("--raw", default="/tmp/s5-console.raw")
a = ap.parse_args()

ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[()][A-Z0-9]|\r")
PROMPT = re.compile(r"(?:^|\n)[^\n]*[#\$]\s*$")


class Port:
    def __init__(self, node):
        self.fd = os.open(node, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        tty.setraw(self.fd)
        f = fcntl.fcntl(self.fd, fcntl.F_GETFL)
        fcntl.fcntl(self.fd, fcntl.F_SETFL, f & ~os.O_NONBLOCK)
        self.log = bytearray()

    def send(self, data):
        os.write(self.fd, data.encode())

    def drain(self, seconds, until=None):
        """Listen for `seconds`, stopping early when `until` matches the text."""
        end = time.time() + seconds
        start = len(self.log)
        while time.time() < end:
            ready, _, _ = select.select([self.fd], [], [], 0.4)
            if ready:
                try:
                    chunk = os.read(self.fd, 8192)
                except OSError as e:
                    if e.errno in (errno.EAGAIN, errno.EIO):
                        continue
                    raise
                if chunk:
                    self.log += chunk
            text = ANSI.sub("", self.log[start:].decode("utf-8", "replace"))
            if until and until.search(text):
                return text
        return ANSI.sub("", self.log[start:].decode("utf-8", "replace"))

    def close(self):
        os.close(self.fd)


def clean(s):
    return ANSI.sub("", s).replace("\r\n", "\n").strip()


SHELLPROMPT = re.compile(r"[#\$]\s*$")

p = Port(a.node)
try:
    # The console is fast at rest and slow under load, so ask with a bare Enter
    # and wait properly for the answer before concluding anything. A shell that
    # is already there will just print a fresh prompt, and a login prompt will
    # reprint itself; either way one Enter distinguishes them without typing a
    # login name into a shell that does not want one.
    p.send("\r")
    got = clean(p.drain(a.login_wait, until=SHELLPROMPT))
    if SHELLPROMPT.search(got):
        print("  --- already at a shell prompt, not logging in again ---")
    else:
        if re.search(r"[Pp]assword", got):
            print("  the phone asked for a password")
            print("  STOP: the blanking of /etc/shadow did not land, and this will not")
            print("  type a password. It has to be typed by a person.")
            sys.exit(2)
        p.send("root\r")
        got = clean(p.drain(a.login_wait, until=SHELLPROMPT))
        print("  --- after login ---")
        print(got)
    if not SHELLPROMPT.search(got):
        print("  no shell prompt; stopping here rather than typing on")
        sys.exit(3)

    for n, cmd in enumerate(a.commands, 1):
        p.send(cmd + "\r")
        got = clean(p.drain(a.cmd_wait, until=re.compile(r"S5END%d\b" % n)))
        print(f"  --- {cmd} ---")
        print(got)

    # The marker proves the shell finished, but the *echo* of a long command
    # can still be trickling back after it, and closing the port mid-echo throws
    # those bytes away. Give the line discipline a moment to finish before the
    # fd goes away, or the last command is the one that comes back truncated.
    trailing = clean(p.drain(12.0))
    if trailing:
        print("  --- trailing ---")
        print(trailing)
finally:
    p.close()

with open(a.raw, "wb") as f:
    f.write(p.log)
os.chmod(a.raw, stat.S_IRUSR | stat.S_IWUSR)
print(f"  {len(p.log)} byte(s) of transcript in {a.raw}, mode 0600")
