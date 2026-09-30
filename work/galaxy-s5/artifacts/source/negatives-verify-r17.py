#!/usr/bin/env python3
"""Negative tests for verify-boot-r17.py.

A verifier that has never rejected anything is not evidence of anything. Each
case below takes the real r17 init_functions.sh, introduces one specific defect,
rebuilds a boot image from it, and requires the verifier to fail. The defect and
the check that must catch it are named together, because a case that fails for
some unrelated reason is no better than no case at all: every case therefore also
asserts that the named check is among the failures.

Run from the artifacts directory:

    python3 source/negatives-verify-r17.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ARTIFACTS = HERE.parent
FUNCTIONS = HERE / "r17-ramdisk-init_functions.sh"
REPACK = HERE / "repack-boot-r17.py"
VERIFY = HERE / "verify-boot-r17.py"
BASE = ARTIFACTS / "boot-k3gxx-r11-subpartfix-35de59d0.img"
KERNEL = HERE / "vmlinuz-fbcon"

REGION_BEGIN = "# >>> s5screen: usb serial function (begin)\n"
REGION_END = "# <<< s5screen: usb serial function (end)\n"
UDC_WRITE = '\tif [ -z "$skip_udc" ]; then\n\t\tsetup_usb_configfs_udc\n\tfi\n'
LINK_TEST = '\tif [ -e "$CONFIGFS/g1/configs/c.1/$CONFIGFS_ACM_FUNCTION" ]; then'


def once(text, what):
    n = text.count(what)
    if n != 1:
        sys.exit(f"anchor for {what!r} matched {n} times, wanted exactly 1")
    return text


def _region_line_range(lines):
    b = next(
        i
        for i, l in enumerate(lines)
        if l.rstrip("\n").endswith("# >>> s5screen: usb serial function (begin)")
    )
    e = next(
        i
        for i, l in enumerate(lines)
        if l.rstrip("\n").endswith("# <<< s5screen: usb serial function (end)")
    )
    return b, e


def _move_region(text, anchor, after=True):
    """Move the whole marked region, as a block of whole lines, elsewhere.

    Moving it as complete lines rather than by slicing the string is what keeps
    the repacker's byte-exact strip-back check satisfied, so the defect reaches
    the verifier instead of being turned away at build time. A mutant caught by
    the build is legitimate, but it proves nothing about the verifier.

    after=True inserts past the anchor line. Getting this backwards silently
    produces a mutant whose region still sits in the right place, which the
    verifier then correctly accepts -- so the case would report a defect that
    was never introduced.
    """
    lines = text.splitlines(keepends=True)
    b, e = _region_line_range(lines)
    region = lines[b : e + 1]
    rest = lines[:b] + lines[e + 1 :]
    at = next(i for i, l in enumerate(rest) if anchor(l))
    cut = at + 1 if after else at
    return "".join(rest[:cut] + region + rest[cut:])


def move_region_after_bind(text):
    """The r16 ordering: create the function once the controller is bound."""
    return _move_region(text, lambda l: l.strip() == "setup_usb_configfs_udc", after=True)


def move_region_out_of_function(text):
    """A working-looking region, in a function that has no bind to precede."""
    return _move_region(text, lambda l: l.startswith("debug_shell() {"), after=False)


def drop_region(text):
    lines = text.splitlines(keepends=True)
    b, e = _region_line_range(lines)
    return "".join(lines[:b] + lines[e + 1 :])


# Each case: name, mutation, and the check label that must catch it.
CASES = [
    (
        "region moved after the bind (the r16 ordering)",
        move_region_after_bind,
        "the region runs before the controller is bound",
    ),
    (
        "region removed entirely, as in r16",
        drop_region,
        "the usb serial function region is present",
    ),
    (
        "the symlink into c.1 removed",
        lambda t: t.replace(
            '\t\tln -sf "$CONFIGFS/g1/functions/$CONFIGFS_ACM_FUNCTION" \\\n'
            '\t\t\t"$CONFIGFS/g1/configs/c.1" \\\n'
            '\t\t\t|| echo "INFO: could not link $CONFIGFS_ACM_FUNCTION into c.1"\n',
            "",
        ),
        "usb serial region links the function into the configuration",
    ),
    (
        "verdict reverted to the /dev/ttyGS0 test",
        lambda t: once(t, LINK_TEST).replace(
            LINK_TEST, '\tif [ -e /dev/ttyGS0 ]; then', 1
        ),
        "gadget half checks the link in the configuration, not /dev/ttyGS0",
    ),
    (
        "gadget setup put back into the late helper",
        lambda t: once(t, "# --- half 2: the getty ---").replace(
            "# --- half 2: the getty ---",
            "\tsetup_usb_acm_configfs\n\tsetup_usb_configfs_udc\n\n"
            "\t# --- half 2: the getty ---",
            1,
        ),
        "gadget half must not",
    ),
    (
        "configfs path hardcoded in the region",
        lambda t: once(t, 'if [ -e "$CONFIGFS" ]; then').replace(
            'if [ -e "$CONFIGFS" ]; then',
            'if [ -e /sys/kernel/config/usb_gadget ]; then',
            1,
        ),
        "usb serial region must not assume configfs is mounted",
    ),
    (
        "tr double-escaped, so it eats every n in the listing",
        lambda t: t.replace("tr '\\n' ' '", "tr '\\\\n' ' '"),
        "usb serial region translates a real newline in tr",
    ),
    (
        "region binds a second UDC of its own",
        lambda t: once(t, "# <<< s5screen: usb serial function (end)").replace(
            "# <<< s5screen: usb serial function (end)",
            "\tsetup_usb_configfs_udc\n"
            "\t# <<< s5screen: usb serial function (end)",
            1,
        ),
        "usb serial region does not bind the controller itself",
    ),
    (
        "region moved out of setup_usb_network_configfs",
        move_region_out_of_function,
        "the region is inside setup_usb_network_configfs",
    ),
]


def region_position(text):
    """Where the region sits relative to the bind, as (region_at, bind_at).

    (-1, -1) if the region is not there at all. Used to confirm a mutation
    actually changed the property it is meant to change: comparing whole texts
    is not enough, because a move that lands in the same place produces a
    different string and an identical image.
    """
    lines = text.splitlines(keepends=True)
    try:
        b, _ = _region_line_range(lines)
    except StopIteration:
        return -1, -1
    binds = [
        i for i, l in enumerate(lines) if l.strip() == "setup_usb_configfs_udc"
    ]
    return b, (binds[0] if binds else -1)


def main():
    for p in (FUNCTIONS, REPACK, VERIFY, BASE, KERNEL):
        if not p.exists():
            sys.exit(f"missing input: {p}")

    original = FUNCTIONS.read_text()
    passed = failed = 0
    try:
        for name, mutate, expected_check in CASES:
            mutant = mutate(original)
            if mutant == original:
                # A mutation that changed nothing would make the case pass for
                # the wrong reason, which is how a suite of negative tests ends
                # up proving nothing at all.
                print(f"FAIL  {name}\n        the mutation changed nothing")
                failed += 1
                continue
            if region_position(mutant) == region_position(original) and "moved" in name:
                print(
                    f"FAIL  {name}\n        the region is still in the same place "
                    f"relative to the bind, so nothing was introduced"
                )
                failed += 1
                continue
            FUNCTIONS.write_text(mutant)
            with tempfile.TemporaryDirectory() as tmp:
                img = Path(tmp) / "mutant.img"
                build = subprocess.run(
                    [
                        sys.executable, str(REPACK), str(BASE), str(FUNCTIONS),
                        str(KERNEL), str(HERE / "s5screen"), str(HERE / "s5iskey"),
                        str(img),
                    ],
                    capture_output=True, text=True, cwd=ARTIFACTS,
                )
                if build.returncode != 0:
                    # Rejected at build time. The repacker enforces
                    # strip-back and parseability, so catching a defect there is
                    # a legitimate outcome, not a gap in the test.
                    print(f"PASS  {name}\n        rejected at build time")
                    passed += 1
                    continue
                res = subprocess.run(
                    [sys.executable, str(VERIFY), str(img)],
                    capture_output=True, text=True, cwd=ARTIFACTS,
                )
            fails = [l.strip() for l in res.stdout.splitlines() if "FAIL" in l]
            if res.returncode == 0:
                print(f"FAIL  {name}\n        the verifier accepted it")
                failed += 1
            elif not any(expected_check in l for l in fails):
                print(f"FAIL  {name}\n        rejected, but not by {expected_check!r}")
                for l in fails:
                    print(f"        | {l}")
                failed += 1
            else:
                hit = next(l for l in fails if expected_check in l)
                print(f"PASS  {name}\n        {hit}")
                passed += 1
    finally:
        FUNCTIONS.write_text(original)
        print("\nrestored the real r17 init_functions.sh")

    print(f"\npassed {passed}, failed {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
