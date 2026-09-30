#!/usr/bin/env python3
"""Negative tests for verify-boot-r19.py.

A verifier that has never rejected anything is not evidence of anything. Each
case below takes a real r19 source file, introduces one specific defect,
rebuilds a boot image from it, and requires the verifier to fail. The defect and
the check that must catch it are named together, because a case that fails for
some unrelated reason is no better than no case at all: every case therefore also
asserts that the named check is among the failures.

There are three families of case:

  * init_functions.sh mutations, which attack the android_usb design. r18 works
    through android_usb rather than configfs, because android.c owns the
    controller on this device. Most of these attack exactly that, since that is
    the part of the design that r17 got wrong and that nothing but the r17 boot
    log could reveal.
  * CACHE keeper mutations, which attack the r19 delta: the logger that has to
    survive switch_root and re-assert acm afterwards.
  * prior revisions, which have to be rejected outright. r19 with the keeper
    switched off is byte-identical to r18, so if r18 passes, nothing in section
    6 of the verifier is real.

Some keeper defects are caught by the repacker rather than the verifier -- the
repacker refuses to ship a keeper that drops a line from the base logger, or an
init_2nd.sh whose two edits do not reverse cleanly. Those cases are kept, and a
build-time rejection is reported as a pass, because the repacker is part of the
thing being verified.

Run from the artifacts directory:

    python3 source/negatives-verify-r19.py
"""

import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ARTIFACTS = HERE.parent
FUNCTIONS = HERE / "r19-ramdisk-init_functions.sh"
REPACK = HERE / "repack-boot-r19.py"
VERIFY = HERE / "verify-boot-r19.py"
BASE = ARTIFACTS / "boot-k3gxx-r11-subpartfix-35de59d0.img"
KERNEL = HERE / "vmlinuz-fbcon"
# The r19 delta lives in two more files inside diagnostic-ramdisk-r19/.
DIAG_DIR = HERE / "diagnostic-ramdisk-r19"

# Earlier revisions the verifier must reject. r18 is the important one: building
# r19 with --no-diag reproduces r18's image byte for byte, so the keeper checks
# are the only thing standing between r19 and its own immediate parent. If the
# verifier accepts r18, section 6 is not being enforced at all.
PRIOR_REVISIONS = (
    "boot-k3gxx-r18-androidacm-e58eed74.img",
    "boot-k3gxx-r17-acmearly-3b80249b.img",
    "boot-k3gxx-r16-usbserial-94b7d507.img",
    "boot-k3gxx-r15-s5keys-af313da8.img",
    "boot-k3gxx-r11-subpartfix-35de59d0.img",
)

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


# ---------------------------------------------------------------- keeper cases
# The r19 delta is two files, and every one of these mutations is a way the
# keeper can quietly stop doing its job. They are written as text substitutions
# so the case names state the defect rather than a line number that drifts.

DRIFT_GUARD = (
    '\tif [ "$funcs" = "$want" ] && [ "$enab" = "1" ]; then\n'
    '\t\treturn 0\n'
    '\tfi\n'
)
HANDOVER_GUARD = (
    "\t# but deliberately leave the keeper, and its CACHE mount, running.\n"
    '\t[ -e "$stopfile" ] && exit 0\n'
)
CACHE_SIZE_GUARD = '\t[ "$(cat /sys/class/block/mmcblk0p19/size 2>/dev/null)" = 409600 ] || exit 0\n'


def keeper_reasserts_always(s):
    """Drop the drift test, so the keeper re-asserts acm every cycle.

    This is the quiet failure: the console still works, the host still sees a
    device reconnect repeatedly, and the log fills with identical transitions.
    """
    assert s.count(DRIFT_GUARD) == 1, "drift guard not found"
    return s.replace(DRIFT_GUARD, "\t# drift test removed by negative case\n")


def keeper_drops_keep_acm(s):
    assert s.count("keep_acm() {") == 1
    return s.replace("keep_acm() {", "keep_nothing() {")


def keeper_drops_android_dir(s):
    assert s.count("android_dir() {") == 1
    return s.replace("android_dir() {", "find_nothing() {")


def keeper_unmounts_in_handover(s):
    """The original bug this whole revision exists to avoid: handing the logger
    over and then dropping the partition it logs to."""
    assert s.count(HANDOVER_GUARD) == 1, "handover guard not found"
    return s.replace(HANDOVER_GUARD, HANDOVER_GUARD + "\tunmount_cache\n")


def keeper_stops_in_handover(s):
    """Setting the stop flag in handover ends the main loop, so the post-root
    half never runs even though handover appears to have succeeded."""
    assert s.count(HANDOVER_GUARD) == 1
    return s.replace(HANDOVER_GUARD, HANDOVER_GUARD + '\ttouch "$stopfile"\n')


def keeper_retagged(s):
    """The puller shouts about the word watchdog, so the tag must not use it.

    The repacker pins TAG=s5keep as well, so this is normally caught at build
    time; it stays as a case because the repacker's assert and the verifier's
    check are independent and either could be lost.
    """
    assert s.count("TAG=s5keep") == 1
    return s.replace("TAG=s5keep", "TAG=s5watchdog")


def keeper_code_mentions_watchdog(s):
    """The word in a comment is fine; the same word in code is not.

    This is the regression test for the code-versus-comment distinction. A check
    written as "watchdog not in keeper" would let this through, and then the
    keeper's own line would make the puller shout on every single capture.
    """
    assert s.count("TAG=s5keep") == 1
    return s.replace("TAG=s5keep", "TAG=s5keep\nwatchdog_span=15")


def keeper_writes_outside_cache(s):
    """A diagnostics process that can write to USERDATA is a liability."""
    assert s.count(HANDOVER_GUARD) == 1
    return s.replace(
        HANDOVER_GUARD,
        HANDOVER_GUARD + '\techo stray > /data/codex-keeper-stamp 2>/dev/null || true\n',
    )


def keeper_drops_cache_size_guard(s):
    """Without the geometry check the logger will mount whatever p19 happens to
    be on, and a repartitioned device would have it format-adjacent writes."""
    assert s.count(CACHE_SIZE_GUARD) == 1, "cache size guard not found"
    return s.replace(CACHE_SIZE_GUARD, "")


def keeper_formats(s):
    assert s.count(HANDOVER_GUARD) == 1
    return s.replace(
        HANDOVER_GUARD,
        HANDOVER_GUARD + "\tmkfs.ext4 -F \"$cache\" || true\n",
    )


def keeper_syntax_error(s):
    """A shell parse error in the keeper stops it starting at all, which loses
    the pre-root log as well as the post-root evidence."""
    return s + "\nfi\n"


KERNEL_CAP_CHECK = (
    '\t\t&& [ "$(filesize "$dir/kernel.log")" -lt "$KERNEL_LOG_MAX" ]; then\n'
)
USBDMESG_CAP_CHECK = (
    '\tif [ "$(filesize "$dir/usb.log")" -lt "$USB_DMESG_MAX" ]; then\n'
)


def keeper_drops_kernel_cap(s):
    """The bug this revision was nearly shipped with.

    Appending the whole 2 MB ring buffer every five seconds with no cap fills the
    200 MB CACHE partition in under ten minutes, after which every write the
    keeper makes starts failing and it takes the rest of the evidence with it.
    """
    assert s.count(KERNEL_CAP_CHECK) == 1, "kernel.log cap check not found"
    return s.replace(KERNEL_CAP_CHECK, "\t\t; then\n")


def keeper_drops_usb_dmesg_cap(s):
    """Without this the filtered ring-buffer lines can crowd out the keeper's own
    transition notes, which are the primary record."""
    assert s.count(USBDMESG_CAP_CHECK) == 1, "usb.log dmesg cap check not found"
    return s.replace(USBDMESG_CAP_CHECK, "\tif true; then\n")


def keeper_drops_base_line(s):
    """Removing a line the base logger had is caught by the repacker, which
    pins the shipped logger as a superset of the one earlier rounds verified."""
    line = "\t\t# Give an interrupted snapshot time to release its temporary file.\n"
    assert s.count(line) == 1
    return s.replace(line, "")


def init2nd_reverts_to_stop(s):
    assert s.count('s5diag handover "$S5_DIAG_PID"') == 1
    return s.replace('s5diag handover "$S5_DIAG_PID"', 's5diag stop "$S5_DIAG_PID"')


def init2nd_drops_kill_guard(s):
    old = '\tif ! [ "$pid" = "1" ] && ! [ "$pid" = "$S5_DIAG_PID" ]; then\n'
    assert s.count(old) == 1, "kill-loop guard not found"
    return s.replace(old, '\tif ! [ "$pid" = "1" ]; then\n')


DIAG_CASES = [
    ("keeper re-asserts acm every cycle instead of only on drift",
     "s5diag", keeper_reasserts_always,
     "only re-asserts on drift"),
    ("keeper lost the function that re-asserts acm",
     "s5diag", keeper_drops_keep_acm,
     "the keeper re-asserts acm"),
    ("keeper lost the android_usb lookup",
     "s5diag", keeper_drops_android_dir,
     "the keeper can find android_usb"),
    ("handover unmounts CACHE (the r18 failure mode, reintroduced)",
     "s5diag", keeper_unmounts_in_handover,
     "handover does not unmount CACHE"),
    ("handover sets the stop flag, so the keeper exits immediately",
     "s5diag", keeper_stops_in_handover,
     "handover does not set the stop flag"),
    ("keeper retagged with the puller's fatal keyword",
     "s5diag", keeper_retagged,
     "tag avoids the puller's fatal keyword"),
    ("keeper code mentions the puller's fatal keyword (comment is fine, code is not)",
     "s5diag", keeper_code_mentions_watchdog,
     "tag avoids the puller's fatal keyword"),
    ("keeper gains a write outside CACHE",
     "s5diag", keeper_writes_outside_cache,
     "every write in the keeper lands on CACHE"),
    ("keeper lost the CACHE geometry guard",
     "s5diag", keeper_drops_cache_size_guard,
     "CACHE guards are all still there"),
    ("keeper lost the kernel.log growth cap (fills CACHE in under ten minutes)",
     "s5diag", keeper_drops_kernel_cap,
     "never appended without a size check first"),
    ("keeper lost the filtered-ring-buffer cap (starves its own notes)",
     "s5diag", keeper_drops_usb_dmesg_cap,
     "has its own, lower cap"),
    ("keeper formats the CACHE partition",
     "s5diag", keeper_formats,
     "never formats"),
    ("keeper no longer parses as shell",
     "s5diag", keeper_syntax_error,
     "parses as shell"),
    ("keeper drops a line the base logger had",
     "s5diag", keeper_drops_base_line,
     "r19 keeper is a superset"),
    ("init_2nd.sh stops the keeper instead of handing it over",
     "init_2nd.sh", init2nd_reverts_to_stop,
     "hands the keeper over"),
    ("init_2nd.sh kills the keeper with the getty shells",
     "init_2nd.sh", init2nd_drops_kill_guard,
     "excluded from the getty shell kill loop"),
]


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


# Section-6 checks that only the r19 keeper can satisfy. r18 differs from r19 in
# nothing else, so r18 has to fail one of these specifically -- if it were
# rejected for an unrelated reason the keeper checks would still be untested.
KEEPER_CHECKS = (
    "init_2nd.sh hands the keeper over",
    "r19 keeper is a superset",
    "the keeper re-asserts acm",
    "every write in the keeper lands on CACHE",
    "only the expected files changed",
)

MUTABLE = (FUNCTIONS, DIAG_DIR / "s5diag", DIAG_DIR / "init_2nd.sh")


def build_and_verify(tmp, expected):
    """Build a mutant image and run the verifier on it.

    Returns (verdict, detail). verdict is "build" if the repacker refused, which
    counts as a rejection because the repacker enforces strip-back, shell
    parseability and the keeper superset property; "accepted" if the verifier
    passed the mutant; "rejected" if the verifier failed it, with the expected
    check among the failures; and "other" if it failed for some other reason,
    which is a failure of this test rather than a pass.
    """
    img = Path(tmp) / "mutant.img"
    build = subprocess.run(
        [sys.executable, str(REPACK), str(BASE), str(FUNCTIONS), str(KERNEL),
         str(HERE / "s5screen"), str(HERE / "s5iskey"), str(img)],
        capture_output=True, text=True, cwd=ARTIFACTS,
    )
    if build.returncode != 0:
        err = (build.stderr.strip().splitlines() or ["no message"])[-1]
        return "build", err
    res = subprocess.run(
        [sys.executable, str(VERIFY), str(img)],
        capture_output=True, text=True, cwd=ARTIFACTS,
    )
    if res.returncode == 0:
        return "accepted", "the verifier accepted it"
    fails = [l.strip() for l in res.stdout.splitlines() if "FAIL" in l]
    hit = next((l for l in fails if expected in l), None)
    if hit is not None:
        return "rejected", hit
    return "other", "; ".join(fails) or "rejected but printed no FAIL lines"


def main():
    for p in (REPACK, VERIFY, BASE, KERNEL, *MUTABLE):
        if not p.exists():
            sys.exit(f"missing input: {p}")

    saved = {p: p.read_text() for p in MUTABLE}
    passed = failed = 0
    try:
        def run(name, expected):
            nonlocal passed, failed
            with tempfile.TemporaryDirectory() as tmp:
                verdict, detail = build_and_verify(tmp, expected)
            if verdict in ("build", "rejected"):
                print(f"PASS  {name}\n        {detail}")
                passed += 1
            elif verdict == "accepted":
                print(f"FAIL  {name}\n        {detail}")
                failed += 1
            else:
                print(f"FAIL  {name}\n        rejected, but not by {expected!r}")
                print(f"        {detail}")
                failed += 1

        print("init_functions.sh (the android_usb design)")
        for name, mutate, expected in CASES:
            mutant = mutate(saved[FUNCTIONS])
            if mutant == saved[FUNCTIONS]:
                print(f"FAIL  {name}\n        the mutation changed nothing")
                failed += 1
                continue
            FUNCTIONS.write_text(mutant)
            run(name, expected)
        FUNCTIONS.write_text(saved[FUNCTIONS])

        print("\nCACHE keeper (the r19 delta)")
        for name, filename, mutate, expected in DIAG_CASES:
            target = DIAG_DIR / filename
            mutant = mutate(saved[target])
            if mutant == saved[target]:
                print(f"FAIL  {name}\n        the mutation changed nothing")
                failed += 1
                continue
            target.write_text(mutant)
            run(name, expected)
            target.write_text(saved[target])

        print("\nprior revisions must be rejected outright")
        for img_name in PRIOR_REVISIONS:
            img = ARTIFACTS / img_name
            if not img.exists():
                print(f"FAIL  {img_name}\n        no such artifact")
                failed += 1
                continue
            res = subprocess.run(
                [sys.executable, str(VERIFY), str(img)],
                capture_output=True, text=True, cwd=ARTIFACTS,
            )
            fails = [l.strip() for l in res.stdout.splitlines() if "FAIL" in l]
            if res.returncode == 0:
                print(f"FAIL  {img_name} accepted\n        it must be rejected")
                failed += 1
                continue
            hit = next((l for l in fails if any(k in l for k in KEEPER_CHECKS)), None)
            if img_name.startswith("boot-k3gxx-r18-") and hit is None:
                print(f"FAIL  {img_name} rejected, but not by any keeper check")
                for l in fails:
                    print(f"        | {l}")
                failed += 1
                continue
            print(f"PASS  {img_name} rejected\n        {hit or fails[0]}")
            passed += 1
    finally:
        for p, text in saved.items():
            p.write_text(text)
        print("\nrestored every r19 source file")

    print(f"\npassed {passed}, failed {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
