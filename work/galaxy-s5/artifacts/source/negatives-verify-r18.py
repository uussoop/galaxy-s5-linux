#!/usr/bin/env python3
"""Negative tests for verify-boot-r18.py.

A verifier that has never rejected anything is not evidence of anything. Each
case below takes the real r18 init_functions.sh, introduces one specific defect,
rebuilds a boot image from it, and requires the verifier to fail. The defect and
the check that must catch it are named together, because a case that fails for
some unrelated reason is no better than no case at all: every case therefore also
asserts that the named check is among the failures.

r18 works through android_usb rather than configfs, because android.c owns the
controller on this device. Most of the cases below attack exactly that, since
that is the part of the design that r17 got wrong and that nothing but the r17
boot log could reveal.

Run from the artifacts directory:

    python3 source/negatives-verify-r18.py
"""

import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ARTIFACTS = HERE.parent
FUNCTIONS = HERE / "r18-ramdisk-init_functions.sh"
REPACK = HERE / "repack-boot-r18.py"
VERIFY = HERE / "verify-boot-r18.py"
BASE = ARTIFACTS / "boot-k3gxx-r11-subpartfix-35de59d0.img"
KERNEL = HERE / "vmlinuz-fbcon"

ANDROID_DEV = "/sys/class/android_usb/android0"
REGION_BEGIN = "# >>> s5screen: usb serial function (begin)\n"
REGION_END = "# <<< s5screen: usb serial function (end)\n"
DISABLE = '\t\techo "0" > "$android_dev/enable" 2>/dev/null \\\n\t\t\t|| echo "INFO: could not disable android_usb"\n'
ASK_ACM = '\t\tif echo acm > "$android_dev/functions" 2>/dev/null; then\n'
REENABLE = '\t\techo "1" > "$android_dev/enable" 2>/dev/null \\\n\t\t\t|| echo "INFO: could not re-enable android_usb"\n'
# The probe for android_usb appears twice, once in the early region and once in
# the late helper, and they have to be told apart: a mutation aimed at the
# early one that lands on the late one leaves the gadget setup untouched and
# would be reported as a defect that was never introduced. The following line
# of the early region is the banner only it prints.
ANDROID_PROBE = (
    '\tif [ -e "$android_dev" ]; then\n'
    '\t\techo "INFO: android_usb owns the controller'
)


def once(text, what):
    n = text.count(what)
    if n != 1:
        sys.exit(f"anchor for {what!r} matched {n} times, wanted exactly 1")
    return text


def use_instance_name(text):
    """Ask android for acm.usb0 instead of acm.

    This is the mistake r17's own shape invites. configfs addresses a function
    as <type>.<instance>; android's functions attribute does not, it splits on
    commas and looks each name up in the driver's table, so "acm.usb0" matches
    nothing and android enables no function at all while reporting no error.
    """
    return once(text, ASK_ACM).replace(
        ASK_ACM, '\t\tif echo acm.usb0 > "$android_dev/functions" 2>/dev/null; then\n', 1
    )


def no_disable_first(text):
    """Rewrite the function list while ncm is still bound.

    android replaces the whole descriptor on the 0 to 1 edge. Setting functions
    while enabled leaves the previously bound configuration in place, so the
    change may never take effect.
    """
    return once(text, DISABLE).replace(DISABLE, "", 1)


def never_reenable(text):
    """Set the function but never bring the gadget back up.

    Silently plausible: every write succeeds, and only the absence of the
    second enable edge keeps the old descriptor on the wire.
    """
    return once(text, REENABLE).replace(REENABLE, "", 1)


def android_after_configfs(text):
    """Try configfs first and only then android.

    Restores the r17 ordering. On this device configfs never gets the
    controller, so the fallback is never reached and the serial port never
    appears, with nothing in the log to say why.
    """
    return once(text, ANDROID_PROBE).replace(
        ANDROID_PROBE,
        '\tif [ -e "$CONFIGFS" ]; then\n\t\t:\n\telif [ -e "$android_dev" ]; then\n'
        '\t\techo "INFO: android_usb owns the controller',
        1,
    )


def android_wrong_node(text):
    """Probe for android_usb under a path this kernel does not have.

    Plausible because the device number is not something anyone would think to
    check: on a kernel with two android controllers android0 is not the only
    possibility, and a probe for the wrong one silently falls through to
    configfs, which never gets the controller.
    """
    head, sep, tail = text.partition(ANDROID_PROBE)
    if not sep:
        sys.exit("android probe anchor not found")
    return head + tail.replace(ANDROID_DEV, "/sys/class/android_usb/android1", 1)


def hardcode_configfs_path(text):
    return once(text, 'elif [ -e "$CONFIGFS" ]; then').replace(
        'elif [ -e "$CONFIGFS" ]; then',
        'elif [ -e /sys/kernel/config/usb_gadget ]; then',
        1,
    )


def configfs_hardcodes_instance(text):
    old = '"$CONFIGFS/g1/functions/$CONFIGFS_ACM_FUNCTION"'
    if old not in text:
        sys.exit(f"anchor {old!r} not found")
    # The mkdir and the ln both name the instance, so both are replaced; the
    # helper's own half 1 uses a different string and is left alone.
    return text.replace(old, '"$CONFIGFS/g1/functions/acm.usb0"')


def drop_symlink(text):
    return once(
        text,
        '\t\tln -sf "$CONFIGFS/g1/functions/$CONFIGFS_ACM_FUNCTION" \\\n'
        '\t\t\t"$CONFIGFS/g1/configs/c.1" \\\n'
        '\t\t\t|| echo "INFO: could not link $CONFIGFS_ACM_FUNCTION into c.1"\n',
    ).replace(
        '\t\tln -sf "$CONFIGFS/g1/functions/$CONFIGFS_ACM_FUNCTION" \\\n'
        '\t\t\t"$CONFIGFS/g1/configs/c.1" \\\n'
        '\t\t\t|| echo "INFO: could not link $CONFIGFS_ACM_FUNCTION into c.1"\n',
        "",
        1,
    )


def tr_double_escaped(text):
    return text.replace("tr '\\n' ' '", "tr '\\\\n' ' '")


UDC_REPORT = (
    'echo "INFO: controllers available: [$(ls /sys/class/udc 2>/dev/null '
    "| tr '\\n' ' ')]\"\n"
)


def drop_udc_report(text):
    """Stop naming the available controllers in the early region.

    The late helper prints the same line, so a plain replace would hit both and
    the mutation would not be the one it claims to be. Only the region's copy
    goes.
    """
    b = REGION_BEGIN
    e = REGION_END
    if text.count(b) != 1 or text.count(e) != 1:
        sys.exit("region markers are not unique")
    head, rest = text.split(b, 1)
    region, tail = rest.split(e, 1)
    if region.count(UDC_REPORT) != 1:
        sys.exit(f"expected one controller report in the region, found {region.count(UDC_REPORT)}")
    return head + b + region.replace(UDC_REPORT, "", 1) + e + tail


VERDICT_ANCHOR = (
    '\tif [ -e "$android_dev" ]; then\n'
    '\t\techo "INFO: android_usb functions:'
)


def verdict_back_to_ttygs0(text):
    """Report the /dev/ttyGS0 node as the verdict again.

    r16's specific mistake. android.c allocates every u_serial port at probe
    time, so that node exists on every boot and says nothing about the wire.
    """
    return once(text, VERDICT_ANCHOR).replace(
        VERDICT_ANCHOR, '\tif [ -e /dev/ttyGS0 ]; then\n\t\techo "INFO: android_usb functions:', 1
    )


CASES = [
    ("android asked for the instance name acm.usb0", use_instance_name,
     "android branch asks for the bare acm name, not an instance name"),
    ("android_usb not disabled before the function list is rewritten", no_disable_first,
     "android branch disables before rewriting the function list"),
    ("android_usb never re-enabled after the function list is set", never_reenable,
     "android branch re-enables after setting the function"),
    ("android_usb tried only after the configfs fallback (the r17 ordering)",
     android_after_configfs, "android branch is tried before the configfs fallback"),
    ("android_usb probed under a node that does not exist", android_wrong_node,
     "android branch is tried before the configfs fallback"),
    ("configfs branch hardcoded the path", hardcode_configfs_path,
     "usb serial region must not assume configfs is mounted"),
    ("configfs branch hardcoded acm.usb0", configfs_hardcodes_instance,
     "configfs branch does not hardcode acm.usb0"),
    ("the symlink into c.1 removed", drop_symlink,
     "usb serial region links the function into the configuration"),
    ("tr double-escaped, so it eats every n in the listing", tr_double_escaped,
     "usb serial region translates a real newline in tr"),
    ("the controller list no longer reported", drop_udc_report,
     "usb serial region reports which controllers exist"),
    ("verdict reverted to the /dev/ttyGS0 test", verdict_back_to_ttygs0,
     "gadget half checks the link in the configuration, not /dev/ttyGS0"),
]


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
                print(f"FAIL  {name}\n        the mutation changed nothing")
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
                    # The repacker enforces strip-back and parseability, so
                    # catching a defect there is legitimate, not a gap.
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
        print("\nrestored the real r18 init_functions.sh")

    print(f"\npassed {passed}, failed {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
