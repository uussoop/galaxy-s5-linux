#!/usr/bin/env python3
"""Mutation tests for the r26 boot image builder.

Two classes, because r26 changed two things that can each be wrong.

The first mutates the initramfs script: a defect that r26's own comments say
must be impossible, re-pinned so the byte-level hash guard cannot be what
catches it, and required to be refused.

The second mutates the builder itself, aimed at the device tree patch. That code
edits a flattened device tree, where a mistake is a phone that does not boot at
all rather than a phone that boots wrong, and where two of the defects below are
ones this file actually shipped during development: an edit written through a
throwaway object that had copied its input, and a confinement bound that was
computed in the wrong coordinate system and so compared the wrong bytes.

A build that succeeds on any of these is a gap in the builder, not a pass.
"""
import hashlib
import pathlib
import re
import subprocess
import sys

F = pathlib.Path("source/diagnostic-ramdisk-r26/init_2nd.sh")
B = pathlib.Path("source/repack-boot-r26.py")
good_f, good_b = F.read_text(), B.read_text()

ARGS = ["boot-k3gxx-r11-subpartfix-35de59d0.img", "source/r22-ramdisk-init_functions.sh",
        "source/vmlinuz-fbcon", "source/s5screen", "source/s5iskey",
        "source/diagnostic-ramdisk-r26/s5usbkeep", "/tmp/s5-neg26.img"]


def build():
    return subprocess.run([sys.executable, "source/repack-boot-r26.py"] + ARGS,
                          capture_output=True, text=True)


def report(name, p):
    out = (p.stdout + p.stderr).strip().splitlines()
    verdict = "REFUSED" if p.returncode else "*** BUILT ANYWAY ***"
    reason = next((l.strip() for l in reversed(out) if "Error" in l), out[-1].strip())
    print("  %-54s %s" % (name, verdict))
    print("     %s" % reason[:140])
    return p.returncode != 0


def mutate_init(name, old=None, new=None, cut=None):
    s = good_f
    if cut:
        a, b = cut
        s = s[:a] + s[b:]
    else:
        assert s.count(old) == 1, "target not unique: %r" % old[:60]
        s = s.replace(old, new, 1)
    F.write_text(s)
    t = re.sub(r'R26_INIT2ND_SHA256 = "[0-9a-f]+"',
               'R26_INIT2ND_SHA256 = "%s"' % hashlib.sha256(F.read_bytes()).hexdigest(),
               good_b, count=1)
    B.write_text(t)
    r = report(name, build())
    F.write_text(good_f)
    B.write_text(good_b)
    return r


def mutate_builder(name, old, new):
    assert good_b.count(old) == 1, "target not unique: %r" % old[:60]
    B.write_text(good_b.replace(old, new, 1))
    r = report(name, build())
    B.write_text(good_b)
    return r


r = []
# --- the getty repair, which is what r26 is for ---
r.append(mutate_init("1. the getty line loses its baud rate again",
                     'print "ttyGS0::respawn:/sbin/getty -L 115200 ttyGS0 vt100"',
                     'print "ttyGS0::respawn:/sbin/getty -L ttyGS0 vt100"'))
r.append(mutate_init("2. the repair is verified by substring",
                     "grep -q '^ttyGS0::respawn:/sbin/getty -L 115200 ttyGS0 vt100$'",
                     "grep -q '115200 ttyGS0'"))
r.append(mutate_init("3. the inittab is not backed up",
                     'if cp -p "$it" "$bak" 2>/dev/null || cp -f "$it" "$bak" 2>/dev/null; then',
                     "if true; then"))
r.append(mutate_init("4. the rewrite reads the live file, not the backup",
                     '$bak" > "$it.s5new"', '$it" > "$it.s5new"'))
r.append(mutate_init("5. a repair that did not land still reports success",
                     'echo "INFO: WARNING the repair did not land, the console will stay mute"',
                     'echo "INFO: getty repaired"'))
r.append(mutate_init("6. a failed rewrite no longer restores the original",
                     'cp -f "$bak" "$it" 2>/dev/null', ":"))
# --- the console ---
r.append(mutate_init("7. the console is never pointed at ttyGS0",
                     'echo ttyGS0 > "$act" 2>/dev/null', 'echo "" > "$act" 2>/dev/null'))
# --- ordering ---
k = good_f.index("\ns5usb_fix_getty\n")
r.append(mutate_init("8. the getty repair moved after switch_root", None, None,
                     cut=(k, k + len("\ns5usb_fix_getty\n"))))
k = good_f.index("\ns5usb_console_on\n")
r.append(mutate_init("9. the console step moved after switch_root", None, None,
                     cut=(k, k + len("\ns5usb_console_on\n"))))
# --- matching services instead of naming them, the r25 bug this photo caught ---
r.append(mutate_init("10. a service name is hardcoded again",
                     "names=$(s5usb_usb_names)", 'names="s5-usb-ocn"'))
r.append(mutate_init("11. runlevel links are tested with -e again",
                     '[ -L "$link" ]', '[ -e "$link" ]'))
r.append(mutate_init("12. unlinking nothing is no longer a warning",
                     'echo "INFO: WARNING nothing was unlinked, so OpenRC may still start them"',
                     'echo "INFO: done"'))
r.append(mutate_init("13. the printed list and the stubbed list diverge",
                     "for name in $names; do\n\t\t\techo \"=== r26: BEGIN",
                     'for name in s5-usb-ocn; do\n\t\t\techo "=== r26: BEGIN'))
r.append(mutate_init("14. nothing matching is passed over quietly",
                     'echo "INFO: WARNING no USB service matched, so acm can still be taken"',
                     'echo "INFO: nothing to do"'))
# --- an r25 regression, to prove the carried-over assertions still bite ---
r.append(mutate_init("15. a bind mount is reintroduced",
                     "\tn=0\n\tlinks=0\n", "\tn=0\n\tlinks=0\n"
                     "\tmount -o bind /tmp/decoy /sys/class/android_usb 2>/dev/null\n"))

# --- the device tree patch, aimed at the builder itself ---
r.append(mutate_builder("16. the dead UART is put back on the command line",
                         'NEW_BOOTARGS = b"console=ttyGS0 vmalloc=512M clk_ignore_unused\\x00"',
                         'NEW_BOOTARGS = b"console=ttySAC2,115200 vmalloc=512M clk_ignore_unused\\x00"'))
r.append(mutate_builder("17. the tree edit is written to a discarded copy",
                         "    tree = fw.blob", "    tree = bytearray(tree)"))
r.append(mutate_builder("18. a byte outside the property is also changed",
                         "    tree = fw.blob",
                         "    tree = fw.blob\n    tree[-1] = tree[-1] ^ 0xFF  # a stray edit"))
r.append(mutate_builder("19. the bootargs patch is skipped entirely",
                         'fw.set_string_in_place("/chosen", "bootargs", new_ba.rstrip(b"\\x00"))',
                         "pass  # the patch never happens"))
# Controls 20 and 21 replace two that were not defects. The first loosened a
# single pin while four others still constrained the same value, which is
# defence in depth rather than a hole. The second replaced the end-to-end re-read
# with a value that happens to be right, which a build whose device tree really
# is right cannot fail. Neither could be caught, and manufacturing a failure for
# them would have been a fake. What is left is to check the two accidental ways
# the command line itself can come out wrong.
r.append(mutate_builder("20. the command line is written three bytes short",
                         'fw.set_string_in_place("/chosen", "bootargs", new_ba.rstrip(b"\\x00"))',
                         'fw.set_string_in_place("/chosen", "bootargs", new_ba.rstrip(b"\\x00")[:-3])'))
r.append(mutate_builder("21. a wrong console port is shipped",
                         'NEW_BOOTARGS = b"console=ttyGS0 vmalloc=512M clk_ignore_unused\\x00"',
                         'NEW_BOOTARGS = b"console=ttyGS1 vmalloc=512M clk_ignore_unused\\x00"'))

print("\n  %d/%d refused" % (sum(r), len(r)) if all(r) else "\n  *** GAP ***")
sys.exit(0 if all(r) else 1)
