#!/usr/bin/env python3
"""Read a pulled s5diag usb.log and say, in order, what happened to android_usb.

The question this exists to answer is narrow and specific. From the r18 boot we
know three facts: the initramfs bound acm correctly at 3.607 s, it was still acm
at 7.457 s when switch_root ran, and the host eventually saw an ncm-only
descriptor (bcdDevice 0xFFFF) with no CDC ACM interface. So something after
switch_root reconfigured android_usb, and r19 was built to find out what and who.

r19's keeper records every state change it observes into usb.log, and before each
re-assert it copies the last 25 lines of the kernel ring buffer into the same
log. Those 25 lines are the point: they are whatever the kernel printed
immediately before the drift, so a conn_gadget line landing in that window means
Samsung's gadget driver did it, and anything else means the real userland did.
This script separates those two cases explicitly rather than making the reader
eyeball 16 MB of text.

    python3 diagnostics/analyze_usb_log.py usb.log
    python3 diagnostics/analyze_usb_log.py usb.log --detail

It is a pure text reader. It never touches the phone.
"""

import argparse
import re
import sys
from collections import Counter

# The keeper's own line vocabulary, from artifacts/source/diagnostic-ramdisk-r19/s5diag.
STAGE_RE = re.compile(r"^\[\s*([0-9.]+)s?\]\s*(.*)$")
KEEP_RE = re.compile(r"^(s5keep)\b")

# Ring-buffer lines the keeper copied in before a re-assert. `dmesg` writes a
# priority stamp and then the timestamp with no space between them, e.g.
# "<6>[  31.884201] conn_gadget: ...", so the stamp and the "[ t]" are both
# optional suffixes rather than a delimiter to skip past.
RING_RE = re.compile(r"^<[0-9]+>\s*\[?\s*[0-9]+\.[0-9]+\]?\s*(.*)$")
RING_BARE_RE = re.compile(r"^<[0-9]+>\s*(.*)$")

# Markers the keeper writes into the ring buffer too, so that a live
# `dmesg | tail` on a phone you have a shell into shows the same story.
NOTE_RE = re.compile(r"s5keep:")

# Which actor reconfigured android_usb? The two candidates are told apart by a
# fact about the kernel's own code rather than by guesswork.
#
# conn_gadget is Samsung's in-kernel gadget multiplexer. It reconfigures
# android_usb from inside the kernel, driven by cable/VBUS state, and no
# userspace process asked it to. If it shows up in the window immediately
# before a drift, the rebind was the kernel acting on its own and a keeper
# process can only lose a race against it.
CONN_GADGET = re.compile(r"conn_gadget|conn_usb|usb:\s*conn_", re.I)

# A write to android_usb's configfs attributes can only come from userspace:
# functions_store and enable_store are the store handlers for the `functions`
# and `enable` attributes, and the only way to reach a configfs store handler
# is an open()/write() from a process. So their presence in the window is
# positive proof that the drift was requested by something in the real system,
# not by the kernel spontaneously.
CONFIGFS_WRITE = re.compile(r"functions_store|enable_store|configfs", re.I)

# Named userspace services, for the case where the log names the culprit.
USERLAND_HINT = re.compile(
    r"init\.rc|init\.d/|/etc/init|usbmuxd|on-property|ueventd"
    r"|Starting service|setprop|getprop",
    re.I,
)

# Generic USB-stack noise, used only to say "this window is about USB but the
# actor is not named".
SAMSUNG_USB = re.compile(r"dwc3|\budc\b|usb_gadget|android_usb|ffs_acm|\bacm\b", re.I)


def parse(lines):
    """Split usb.log into keeper events and ring-buffer bursts.

    The two kinds of line are told apart by shape, not by position, because the
    keeper writes them in one file but they are not interleaved in any fixed
    order. A keeper line is "[ <t>s] s5keep: ...". A ring-buffer line is a
    kernel priority stamp, "<6>[ <t>] ...", which is what `dmesg` emits and
    therefore what ends up inside a captured burst.
    """
    events = []
    rings = []
    cur = None  # the window being filled, or None

    def close(at_ts):
        nonlocal cur
        if cur is not None:
            cur["at"] = at_ts
            cur = None

    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            close("end of file")
            continue

        m = STAGE_RE.match(stripped)
        if m and not RING_BARE_RE.match(stripped):
            # a keeper line: "[ <t>s] <body>". It terminates any burst in
            # progress and is itself the timestamp for that burst, because the
            # keeper writes the burst immediately before the re-assert it is
            # about to do.
            ts, body = m.group(1), m.group(2)
            close(f"{ts}s (keeper clock; kernel stamps are inside the window)")
            events.append((ts, body))
            continue

        rm = RING_RE.match(stripped) or RING_BARE_RE.match(stripped)
        if rm:
            if cur is None:
                cur = {"at": "?", "lines": []}
                rings.append(cur)
            cur["lines"].append(rm.group(1).strip())
            continue

        # anything else is a continuation of the burst in progress
        if cur is not None:
            cur["lines"].append(stripped)

    close("end of file")
    return events, rings


def verdict_for(ring):
    """Which actor does this ring-buffer window implicate?

    Order matters. conn_gadget is checked first because Samsung's driver does
    its own functions_store/enable_store calls when it reconfigures, so a
    window containing both is a kernel-side rebind that merely looks like a
    configfs write, and calling it userspace would send the next revision down
    the wrong path.
    """
    blob = "\n".join(ring)
    if CONN_GADGET.search(blob):
        return "KERNEL/SAMSUNG conn_gadget", _dedup(CONN_GADGET.findall(blob))
    if CONFIGFS_WRITE.search(blob):
        who = "USERSLAND (real system re-ran USB setup)"
        hits = _dedup(CONFIGFS_WRITE.findall(blob) + USERLAND_HINT.findall(blob))
        return who, hits
    if USERLAND_HINT.search(blob):
        return "USERSLAND (real system re-ran USB setup)", _dedup(USERLAND_HINT.findall(blob))
    if SAMSUNG_USB.search(blob):
        return "USB stack, actor not identified", _dedup(SAMSUNG_USB.findall(blob))
    return "nothing recognisable", []


def _dedup(hits):
    seen, out = set(), []
    for h in hits:
        if h.lower() not in seen:
            seen.add(h.lower())
            out.append(h)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", help="the pulled usb.log")
    ap.add_argument("--detail", action="store_true",
                    help="print every ring-buffer line, not just the verdict")
    args = ap.parse_args()

    try:
        with open(args.path, errors="replace") as fh:
            lines = fh.readlines()
    except OSError as exc:
        print(f"cannot read {args.path}: {exc}", file=sys.stderr)
        return 2

    events, rings = parse(lines)
    if not events:
        print(f"{args.path}: no keeper lines found. Either the keeper never ran,")
        print("or this is not a keeper-written usb.log. Check `stage` and the file size.")
        return 1

    print(f"{args.path}: {len(events)} keeper lines, {len(rings)} ring-buffer windows\n")

    states = Counter()
    drift = reassert = keep = 0
    for ts, body in events:
        if "DRIFT" in body:
            drift += 1
        elif "RE-ASSERT" in body or "re-assert" in body:
            reassert += 1
        elif "functions=" in body:
            states[body.split("functions=")[-1].split()[0].strip("[]")] += 1
            keep += 1

    print("what the keeper saw in android_usb/functions:")
    for func, n in states.most_common():
        print(f"    {func or '(empty)':20} {n} samples")
    print(f"\ndrifts: {drift}   re-asserts: {reassert}   ordinary samples: {keep}")

    if not rings:
        print("\nNo ring-buffer windows were captured, so nothing was actually seen")
        print("drifting. The keeper ran and found the function already correct:")
        print("either the drift happened before handover, or it never happened at all.")
        return 0

    print("\nring-buffer windows captured immediately before each re-assert:")
    tally = Counter()
    for window in rings:
        ring = window["lines"]
        who, hits = verdict_for(ring)
        tally[who] += 1
        print(f"\n  --- at {window['at']} : {who}")
        if hits:
            print("      matched:", ", ".join(hits))
        if args.detail:
            for text in ring:
                print("      |", text)

    print("\nsummary of what the evidence implicates:")
    for who, n in tally.most_common():
        print(f"    {n:3} window(s)  {who}")

    kernel_side = tally.get("KERNEL/SAMSUNG conn_gadget", 0)
    user_side = tally.get("USERSLAND (real system re-ran USB setup)", 0)

    print()
    if kernel_side and user_side:
        print("=> Both actors appear. Read the timestamps: whichever window comes")
        print("   first is the real trigger, and the other is likely a consequence")
        print("   or a second actor reacting to it. --detail shows the order.")
    elif kernel_side:
        print("=> The kernel reconfigured android_usb on its own, after switch_root,")
        print("   with no userspace request. A keeper process is the wrong tool here:")
        print("   it can only lose a race against the driver. The next revision has")
        print("   to stop conn_gadget rather than out-run it.")
    elif user_side:
        print("=> The real system re-ran its own USB setup after switch_root. That is")
        print("   a one-shot action, so a surviving keeper that re-asserts acm wins")
        print("   permanently. The question then is why the keeper did not re-assert,")
        print("   which `stage` answers: it records alive or gone at handover.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
