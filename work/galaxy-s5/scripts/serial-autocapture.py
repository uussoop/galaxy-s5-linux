#!/usr/bin/env python3
"""Watch for the phone's USB serial console and capture the whole window.

Why this exists
---------------
The phone's USB console is a moving target: it appears roughly five minutes
into a postmarketOS boot and is then taken away at a fixed point, measured at
2 min 53 s (r20), 2 min 55 s (r21) and 2 min 54 s (r23). That window is the
only chance to see anything, and asking a person to be at a terminal at the
right second, repeatedly, is a bad way to gather evidence. The failure mode is
already on the record: r20, r21 and r23 each ended with a login prompt
arriving and being missed.

So this does the watching and the typing. It waits for the device node, opens
it, records everything the phone sends until the node disappears, and
optionally logs in and runs one read-only snapshot. Nothing has to be timed by
hand and nothing has to be typed by hand.

How the port is opened
----------------------
Directly, with a retry, rather than through cu. Two reasons. cu needs a lock
file and prints a banner, and the whole point here is to run unattended with
nothing to click. And the direct path is the one that can be tested: this
program can be pointed at a pty, which stands in for the phone, so the
mechanism gets exercised before a boot is spent on it. cu cannot be tested
that way, because it refuses to lock a device it did not create.

Opening a USB serial device on macOS has one real trap. The first open
typically fails with EAGAIN while the driver finishes attaching, and the
device only becomes readable a moment later. So the open is retried for a
while, and a failed attempt is not treated as a dead port. cu is kept as an
automatic fallback, because it is the path that has been proven by hand on
this phone, and losing it silently would be a poor trade.

Safety
------
Two deliberate differences from scripts/serial-watch.py, which watches a human
session and therefore must never touch the port:

  * It opens the port and it types. That is the entire purpose, and it is why
    this is a separate program with a name that says so. The r24 image has no
    root password and no login prompt, so there is nothing for it to type
    over. Do not point it at an image where a person is typing a password.
  * It never prints captured bytes to the terminal. Output goes to two files:
    a raw log and a masked one, using the same masker as
    diagnostics/capture_last_kmsg.py, so anything shaped like a secret is
    redacted before it is ever shown. The raw log is mode 0600 and is not the
    file to read.
"""

from __future__ import annotations

import argparse
import errno
import glob
import importlib.util
import os
import pty
import re
import select
import signal
import subprocess
import sys
import termios
import time
import tty
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SANITIZER = os.path.join(os.path.dirname(HERE), "diagnostics", "capture_last_kmsg.py")

# The two prompts have to be told apart, because it decides what may be typed.
# A login prompt means a password may still be required, and a shell prompt
# means the password is behind us. Only a shell prompt unlocks the snapshot:
# if r24's edit of /etc/shadow had silently failed, the snapshot would
# otherwise be typed straight into a password field, burning the window on a
# failed login. Text typed into a password field is also the one thing here
# that could be read back as a credential, so it is not worth the risk.
PROMPT_LOGIN = re.compile(r"login:\s*$", re.IGNORECASE)
PROMPT_SHELL = re.compile(r"(~ ?#|~ ?\$|#)\s*$")

# One read-only snapshot, run after logging in. It answers the two questions
# that decide what happens next: is there a touchscreen behind /dev/input, and
# what is PID 1 in the real system. The second one matters because r20 and r21
# both installed an inittab respawn line, verified it on disk, and the keeper
# never ran, which means PID 1 did not read that file.
# r26: the paths point at r26, not r24. r24's snapshot was aimed at answering
# whether a shell existed and whether a touchscreen did. Both are answered. What
# is open now is narrower and this snapshot is aimed at it: did the getty repair
# land in the file PID 1 is actually using, is the kernel console really on
# ttyGS0 or did the real system turn it back off as it announced it would, and
# which USB services exist and what is in the boot runlevel.
#
# It also prints, for every runlevel and every USB-shaped service, whether the
# r26 runlevel removal actually removed anything, since the whole point of the
# -L fix is that r25's -e test matched nothing and said nothing about it.
SNAPSHOT = (
    "{ echo '=== who am i ==='; id; "
    "echo '=== PID 1 ==='; cat /proc/1/comm; tr '\\0' ' ' < /proc/1/cmdline; echo; "
    "echo '=== inittab, and which getty line is live ==='; cat /etc/inittab 2>&1; "
    "echo '=== is the kernel console on ttyGS0 ==='; "
    "cat /sys/class/tty/console/active 2>&1; "
    "echo '=== android_usb right now ==='; "
    "cat /sys/class/android_usb/android0/functions 2>&1; "
    "cat /sys/class/android_usb/android0/enable 2>&1; "
    "echo '=== USB-shaped services, and whether they are still ours ==='; "
    "for f in /etc/init.d/*usb* /etc/init.d/*ocn* /etc/init.d/*ncm* /etc/init.d/*acm*; do "
    "  [ -e \"$f\" ] || continue; echo \"--- $f ---\"; head -6 \"$f\"; done 2>&1; "
    "echo '=== runlevel links, and any .s5bak left beside them ==='; "
    "ls -l /etc/runlevels/boot 2>&1 | head -40; "
    "ls -l /etc/init.d/*.s5bak 2>&1; "
    "echo '=== the r26 dump, if it landed on CACHE ==='; "
    "ls -l /cache/codex-s5-diagnostics/r26/ 2>&1; "
    "} > /cache/codex-s5-diagnostics/r26/live.txt 2>&1; "
    "echo SNAPSHOT_DONE; cat /cache/codex-s5-diagnostics/r26/live.txt"
)


def stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


# A serial console ends its lines with a bare carriage return, not a newline,
# and it does not reliably send one at a time: a whole boot can arrive in a
# single read. Splitting only on newlines, as str.splitlines does for a string
# that has none, therefore produces one enormous "line" for the entire capture.
# That is not cosmetic. The masker drops a whole line when it sees a word like
# "password", so a login prompt arriving in the same read as the message after
# it takes the message down with it -- which is how "Login incorrect", the one
# line that says a password is still set, disappeared from a transcript in
# testing. So split on CR as well as LF, and chunk anything with no break in it
# at all, so a phone that never sends a break cannot defeat the masker by
# refusing to end its lines.
_BREAK = re.compile(r"\r\n|\r|\n")
CHUNK = 160


def split_console_lines(data: bytes) -> list[str]:
    text = data.decode("utf-8", "replace")
    pieces = [piece.strip() for piece in _BREAK.split(text)]
    out: list[str] = []
    for piece in pieces:
        if not piece:
            continue
        while len(piece) > CHUNK:
            out.append(piece[:CHUNK])
            piece = piece[CHUNK:]
        if piece:
            out.append(piece)
    return out


def load_masker():
    """Reuse the credential masker rather than writing a second one."""
    spec = importlib.util.spec_from_file_location("s5sanitize", SANITIZER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Log:
    """Raw capture to one file, masked view to another, written as it arrives.

    Masking per line rather than at the end means the readable log is complete
    even if this process is killed mid-window, which is the normal way it ends.
    """

    def __init__(self, raw_path: str, clean_path: str, sanitizer):
        self.sanitizer = sanitizer
        self.redactions = 0
        self.raw = open(raw_path, "wb", buffering=0)
        os.chmod(raw_path, 0o600)
        self.clean = open(clean_path, "a", buffering=1)

    def write(self, data: bytes) -> None:
        self.raw.write(data)
        for line in split_console_lines(data):
            if sanitizer_line_is_sensitive(self.sanitizer, line):
                self.redactions += 1
                # Say how much was lost rather than dropping it silently. A
                # phone that asks for a password and then answers on the same
                # line, with no carriage return between them, puts "Login
                # incorrect" in a line that also contains the word "Password",
                # and that whole line is dropped. The boot evidence all arrives
                # newline-delimited, so it is unaffected, but a reader has to be
                # able to tell "nothing was said" from "something was dropped".
                self.clean.write(
                    f"[{stamp()}] [redacted sensitive line, {len(line)} chars, "
                    f"dropped whole because it mentions a credential]\n")
                continue
            masked = self.sanitizer.mask_values(line)
            if masked != line:
                self.redactions += 1
            self.clean.write(f"[{stamp()}] {masked}\n")

    def note(self, message: str) -> None:
        self.clean.write(f"[{stamp()}] === {message}\n")

    def close(self) -> None:
        for handle in (self.raw, self.clean):
            try:
                handle.close()
            except OSError:
                pass


def sanitizer_line_is_sensitive(mod, line: str) -> bool:
    """Whether the line is one the sanitizer drops outright.

    capture_last_kmsg.sanitize replaces a whole line when it matches
    CRED_WORD, which covers things like a wpa_supplicant invocation where the
    value is on the following line. The word list is reused rather than
    restated, so widening the sanitizer there widens it here too. The module is
    passed in rather than loaded here, because this runs on every line of the
    capture and reloading a module that often would cost more than the capture.
    """
    return bool(mod.CRED_WORD.search(line))


def open_device(node: str, patience: float = 25.0) -> int:
    """Open a serial device, surviving macOS's slow first attach.

    The first open on a USB serial device commonly returns EAGAIN, EWOULDBLOCK
    or EBUSY while the driver settles. Treating that as a dead port is the
    classic mistake, so it is retried until the deadline and only then
    reported.

    What this deliberately does not do is wait for the port to become readable
    before handing it back. That wait is a real trick on some USB serial
    devices, but it is a trap when the other end is simply quiet: r24 silences
    the initramfs console, so for the first seconds of the window the phone
    sends nothing at all and is not going to until a getty asks. Waiting for
    readability there means the open times out on a perfectly healthy console
    and falls through to cu, which cannot lock the device either. A quiet port
    is a port to read, not a port that is broken, so the read loop is left to
    wait, where select already does the right thing.
    """
    deadline = time.time() + patience
    last: OSError | None = None
    while time.time() < deadline:
        try:
            fd = os.open(node, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as exc:
            last = exc
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EBUSY,
                             errno.EINTR, errno.ENXIO):
                time.sleep(0.4)
                continue
            raise
        try:
            tty.setraw(fd, termios.TCSANOW)
        except termios.error as exc:
            os.close(fd)
            raise OSError(errno.EIO, f"the device would not go into raw mode: {exc}")
        return fd
    raise last if last else OSError(errno.EIO, "could not open " + node)


def read_loop(fd: int, node: str, log: Log, deadline: float,
              responders: list | None = None) -> str:
    """Record the phone's output until the window closes. Returns why it ended."""
    last_data = time.time()
    why = "the recording window ran out"
    while time.time() < deadline:
        try:
            ready, _, _ = select.select([fd], [], [], 0.5)
        except OSError as exc:
            why = f"select failed: {exc}"
            break
        if ready:
            try:
                data = os.read(fd, 65536)
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    continue
                why = (f"the port hung up ({exc.strerror})"
                       if exc.errno in (errno.EIO, errno.EBADF) else str(exc))
                break
            if not data:
                why = "the port closed"
                break
            log.write(data)
            last_data = time.time()
            if responders:
                for responder in responders:
                    responder.fed(data.decode("utf-8", "replace"))
        for responder in responders or ():
            responder.tick()
        if not os.path.exists(node):
            why = "the device node disappeared: the window is closed"
            break
        if time.time() - last_data > 120:
            why = "120 s with nothing from the phone"
            break
    return why


def rebind(responder, fd: int) -> None:
    """Point a responder at a different fd.

    The cu fallback runs on a pty rather than on the device, so the responders
    built for the direct path are holding the wrong descriptor. Redirecting
    them is the whole difference between the fallback typing into the phone and
    the fallback typing into a file descriptor nobody reads.
    """
    responder.fd = fd


class Typing:
    """Sends the login sequence, but only once the phone has spoken.

    Timing this on a clock does not work, and the window proved it. The console
    comes up and then says nothing at all for a minute or more while the real
    system boots, because r24 silences the initramfs the way upstream does.
    The login prompt arrives late in the window, near the end, not at the
    start. Anything typed on a timer is therefore typed into a void before the
    getty exists, and the keystrokes sit in the tty buffer to be consumed by
    whatever reads next, which is not a thing to do to a login prompt.

    So the sequence is armed by data instead. Each step waits for the phone to
    speak and for a gap after the previous one, and a step that goes out while
    the phone has been silent for a while is abandoned rather than fired into
    the dark.
    """

    def __init__(self, fd: int, log: Log, steps: list[bytes],
                 gap: float = 2.5, give_up_after: float = 60.0):
        self.fd = fd
        self.log = log
        self.steps = list(steps)
        self.gap = gap
        self.give_up_after = give_up_after
        self.spoke = False
        self.last = time.time()
        self.next_at = 0.0
        self.done = not steps

    def fed(self, text: str) -> None:
        if self.done or not self.steps:
            return
        if not self.spoke:
            self.spoke = True
            self.last = time.time()
            self.next_at = time.time() + self.gap
            self.log.note("the phone spoke, so the login is armed")

    def tick(self) -> None:
        if self.done or not self.steps:
            return
        now = time.time()
        if not self.spoke:
            if now - self.last > self.give_up_after:
                self.done = True
                self.log.note(f"the phone said nothing for {self.give_up_after:.0f} s, "
                              "so nothing was typed")
            return
        if now >= self.next_at:
            payload = self.steps.pop(0)
            try:
                os.write(self.fd, payload)
                self.log.note(f"typed {len(payload)} byte(s)")
            except OSError as exc:
                self.log.note(f"could not type, {exc}")
                self.steps.clear()
            self.next_at = now + self.gap
        if not self.steps:
            self.done = True
            self.log.note("the login sequence is complete")


class Gate:
    """Holds back the snapshot until a shell prompt is actually seen.

    The login name and the bare Enter for the empty password go out on a timer,
    because they cannot hurt. The snapshot does not, because it is long and it
    is the one thing that must not land in a password field. If no shell prompt
    arrives in time the snapshot is dropped and the reason is logged, which is a
    far better outcome than a failed login and a spent window.
    """

    def __init__(self, fd: int, log: Log, snapshot: bytes | None,
                 give_up_after: float = 25.0):
        self.fd = fd
        self.log = log
        self.snapshot = snapshot
        self.give_up_after = give_up_after
        self.started = time.time()
        self.done = snapshot is None
        self.gave_up = False

    def send(self, label: str) -> None:
        try:
            os.write(self.fd, self.snapshot)
            self.log.note(f"{label}: typed {len(self.snapshot)} byte(s)")
        except OSError as exc:
            self.log.note(f"{label}: could not type, {exc}")
        self.done = True

    def fed(self, text: str) -> None:
        if self.done or self.snapshot is None:
            return
        if PROMPT_SHELL.search(text.rstrip()):
            self.send("a shell prompt appeared")

    def tick(self) -> None:
        """Give up on the snapshot if no shell prompt has turned up in time.

        This is a separate call from fed, and it has to be one. Checking the
        deadline only when the phone happens to send something means a phone
        that has gone quiet -- which is exactly what a refused login looks
        like -- never times out, and the snapshot stays armed for the rest of
        the window with nothing to stop it.
        """
        if self.done or self.snapshot is None or self.gave_up:
            return
        if time.time() - self.started > self.give_up_after:
            self.gave_up = True
            self.done = True
            self.log.note("no shell prompt appeared, so the snapshot was NOT "
                          "typed. The root password is probably still set, which "
                          "means r24's edit of /etc/shadow did not land.")


def via_cu(node: str, log: Log, deadline: float,
           responders: list | None = None) -> str:
    """Fallback: drive cu on a pty. Proven by hand on this phone, untestable here.

    cu needs a lock file under /var/spool/uucp and can be denied it, in which
    case it prints "Line in use" and exits without connecting. That is a
    failure of the fallback, not of the port, so it is reported as a reason
    rather than raised, and everything the loop does is guarded: a port that
    hangs up mid-typing raises EIO on write, and an unhandled one of those
    would lose the whole transcript after the useful part.
    """
    why = "cu was not used"
    primary = secondary = None
    runner = None
    try:
        primary, secondary = pty.openpty()
        tty.setraw(primary)
        tty.setraw(secondary)
        runner = subprocess.Popen(["cu", "-l", node], stdin=secondary,
                                  stdout=secondary, stderr=secondary,
                                  close_fds=True, preexec_fn=os.setsid)
        os.close(secondary)
        secondary = None
        why = "the recording window ran out"
        while runner.poll() is None and time.time() < deadline:
            ready, _, _ = select.select([primary], [], [], 0.5)
            if ready:
                try:
                    data = os.read(primary, 65536)
                except OSError as exc:
                    why = f"cu lost the port ({exc.strerror})"
                    break
                if not data:
                    why = "cu saw the port close"
                    break
                log.write(data)
            if data:
                for responder in responders or ():
                    responder.fed(data.decode("utf-8", "replace"))
            for responder in responders or ():
                responder.tick()
            if not os.path.exists(node):
                why = "the device node disappeared: the window is closed"
                break
    except OSError as exc:
        why = f"cu could not be used: {exc}"
        log.note(why)
    finally:
        if runner is not None:
            try:
                os.killpg(os.getpgid(runner.pid), signal.SIGTERM)
            except OSError:
                runner.terminate()
            try:
                runner.wait(timeout=5)
            except subprocess.TimeoutExpired:
                runner.kill()
        for handle in (primary, secondary):
            if handle is not None:
                try:
                    os.close(handle)
                except OSError:
                    pass
    return why


def _first_matching_node(nodes, serial: str | None) -> str | None:
    """The first node in `nodes` that carries `serial`, as a substring.

    Split out of find_node so the matching rule can be tested against a list
    of names rather than against whatever happens to be in /dev when the test
    runs. The rule is one line, and it is a one-line rule that has been wrong
    twice: once tested with endswith, which never matches, and once not tested
    at all, which matched everything. A rule this small earns a named function
    and a test of its own; leaving it inline is what let it drift.
    """
    for name in sorted(nodes):
        if serial and serial in os.path.basename(name):
            return name
    return None


def find_node(serial: str | None, deadline: float) -> str | None:
    """Wait for the phone's node, matching a known USB serial number.

    Several usbmodem nodes come and go during a phone boot: TWRP has one and so
    does pmOS, and they do not necessarily share a serial. Matching on the
    descriptor value keeps this from latching onto the wrong one. The serial is
    a USB descriptor, not a credential.

    The match is a substring, not a prefix or a suffix, and that is not a
    detail. macOS names the node cu.usbmodem<serial><n>, where the trailing n
    only exists to separate two identical devices, so the real name on this
    phone is cu.usbmodem01f44ecab7141 -- the serial is followed by a 1. A
    suffix match against the serial therefore never matches anything, and the
    watcher sat there for the whole of a live console window finding no node.
    """
    announced = False
    while time.time() < deadline:
        found = _first_matching_node(glob.glob("/dev/cu.usbmodem*"), serial)
        if found:
            return found
        if not announced:
            print(f"[{stamp()}] waiting for the USB serial node"
                  + (f" ending in {serial}" if serial else ""))
            announced = True
        time.sleep(1.0)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log", default="/tmp/s5-autocapture.log",
                        help="masked transcript, the one to read (default: %(default)s)")
    parser.add_argument("--raw", default=None,
                        help="raw transcript, mode 0600, not meant to be read")
    parser.add_argument("--serial", default="01f44ecab714",
                        help="only attach to a node ending in this USB serial "
                             "(default: %(default)s)")
    parser.add_argument("--node", default=None,
                        help="attach to this exact path instead of waiting for "
                             "one to appear. This is how the tool is tested "
                             "against a stand-in phone.")
    parser.add_argument("--wait", type=int, default=900,
                        help="seconds to wait for the node (default: %(default)s)")
    parser.add_argument("--run", type=int, default=600,
                        help="seconds to keep recording once attached (default: %(default)s)")
    parser.add_argument("--login", action="store_true",
                        help="type the login sequence. Only safe on an image "
                             "with no root password.")
    parser.add_argument("--snapshot", action="store_true",
                        help="after logging in, run one read-only snapshot to CACHE")
    parser.add_argument("--give-up", type=int, default=90,
                        help="seconds to wait for a shell prompt before refusing "
                             "to type the snapshot (default: %(default)s)")
    parser.add_argument("--force-cu", action="store_true",
                        help="skip the direct open and go through cu")
    args = parser.parse_args()

    if not os.path.exists(SANITIZER):
        print(f"cannot find the masker at {SANITIZER}", file=sys.stderr)
        return 1
    sanitizer = load_masker()

    raw_path = args.raw or os.path.join(
        os.environ.get("TMPDIR", "/tmp").rstrip("/"),
        f"s5-autocapture-{os.getpid()}.raw")
    log = Log(raw_path, args.log, sanitizer)
    log.note(f"raw capture at {raw_path}, mode 0600, do not read")
    print(f"[{stamp()}] masked transcript: {args.log}")
    print(f"[{stamp()}] raw capture:      {raw_path} (0600, not for reading)")

    node = args.node or find_node(args.serial or None, time.time() + args.wait)
    if not node:
        log.note("the node never appeared")
        log.close()
        print(f"[{stamp()}] the node never appeared")
        return 1
    if not os.path.exists(node):
        log.note(f"the given node {node} does not exist")
        log.close()
        return 1

    snapshot = SNAPSHOT.encode() + b"\r" if args.snapshot else None

    started = time.time()
    log.note(f"the window is open on {node}; this is the only chance to see it")
    deadline = time.time() + args.run
    fd = None
    why = "nothing was attempted"
    try:
        if args.force_cu:
            log.note("using cu because it was forced")
            why = via_cu(node, log, deadline, responders)
        else:
            try:
                fd = open_device(node)
                log.note("opened the device directly")
                # Order matters: Typing goes first so the gate starts counting
                # only once the login is genuinely done, and a bare Enter for an
                # empty password cannot be confused with a shell prompt.
                responders = []
                if args.login:
                    responders.append(Typing(fd, log, [b"root\r", b"\r"]))
                if snapshot:
                    responders.append(Gate(fd, log, snapshot,
                                           give_up_after=args.give_up))
                why = read_loop(fd, node, log, deadline, responders)
            except OSError as exc:
                # Losing the proven path silently would be a bad trade, so say
                # what happened and use cu instead. A fallback that cannot
                # connect is reported as the reason, not raised, so that a
                # transcript up to that point is still written and read.
                log.note(f"the direct open failed ({exc}); falling back to cu")
                why = via_cu(node, log, deadline, responders)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        held = time.time() - started
        log.note(f"window held {held:.0f} s and ended because {why}")
        log.note(f"{log.redactions} line(s) masked")
        log.close()
    print(f"[{stamp()}] held {held:.0f} s, ended because {why}")
    print(f"[{stamp()}] {log.redactions} line(s) masked. Read {args.log}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
