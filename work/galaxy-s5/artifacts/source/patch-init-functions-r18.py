#!/usr/bin/env python3
"""Insert the s5screen status display and the check_keys hardening.

Applied to the shipped DM-bypass init_functions.sh. Every change lives inside a
"# >>> s5screen: ... (begin)" / "# <<< s5screen: ... (end)" pair (plus the two
pre-existing dm-bypass pairs) so repack-boot-fbcon.py can strip them all and
assert the remainder is byte-for-byte the base file. If this script's edits ever
drift outside a marked region, that assertion fails instead of silently shipping.

Same ten regions as the s5screen patch, and the marked text is deliberately
unchanged, so the strip-back invariant still reproduces the base byte for byte.
The one behavioural difference is inside keys_still_held, which now calls
s5iskey instead of iskey; that function lives entirely within the helpers
region, so nothing outside it moved.
"""

import sys
from pathlib import Path

HELPERS = '''# >>> s5screen: helpers (begin)
# Default s5keys to "n" so the halt below cannot fire.
#
# This is the only way past the halt on this device, and it has to be set here
# rather than on the kernel command line because the bootloader ignores the BOOT
# image's cmdline field: the r14 kernel log shows the complete 850-byte command
# line the kernel received, and it contains neither "quiet" nor
# "buildvariant=eng", which are the entire contents of that field. Everything
# the kernel sees comes from the device tree's /chosen/bootargs, so an s5keys=n
# written into the boot header is silently discarded.
#
# ${s5keys:-n} rather than a bare assignment so that parse_cmdline, which runs
# after this file is sourced, still wins if a command line ever does carry the
# key. That keeps the documented override working for a future device where the
# bootloader does pass the header through.
#
# What is given up: holding volume-up plus left-shift no longer halts the boot
# and dumps logs, because the real volume-key driver (gpio_keys.16) reports
# KEY_VOLUMEUP held on every sample with nothing pressed, so that check fired on
# every boot and init_2nd.sh blocked at check_keys, never reaching
# mount_subpartitions. The volume-down plus left-control debug shell entry in
# check_keys is above this guard and uses raw iskey, so it still works: there is
# still a way to stop and inspect a bad boot. The underlying phantom on
# gpio_keys.16 is unfixed and remains the real bug.
s5keys="${s5keys:-n}"
# Paint the boot state onto the panel, and stop trusting a single iskey sample.
# Both are diagnostic-only and must never be able to stop a boot.
#
# s5status <state> shows one of: booting, dmok, dmno, loopfail, keys, booted.
# The kernel in this image is built with CONFIG_FRAMEBUFFER_CONSOLE=y, so the
# panel is no longer black and the real system's console is visible. That is
# the fix; this is the part console text cannot do. The console interleaves
# kernel and initramfs output, scrolls, and is gone once switch_root runs, so
# "which of the six things happened" is only ever readable as a single painted
# word that cannot scroll away or be missed. It also covers the window before
# the fb device registers, where there is still no console at all.
# s5screen mmaps /dev/fb0 and paints the state itself: on this device the
# framebuffer has to be written through mmap, because the fbdev accepts a plain
# write(2) but consumes nothing, and rejects msync(2) with EINVAL. Every
# failure inside s5screen is swallowed and it always exits 0, so this can cost
# the screen but never a boot.
# Set nos5screen=y on the kernel command line to switch it off entirely.
# Usage: s5status <state>
# Sets: (none)
# Returns: 0
s5status() {
\t[ "$nos5screen" = "y" ] && return 0
\ts5screen "$1" >/dev/null 2>&1
\treturn 0
}

# Require the given keys to still be reported as held on four consecutive
# samples a second apart before believing that anybody is pressing them, and
# ignore input devices that synthesise keys rather than reading real ones.
#
# Two separate problems are handled here.
#
# First, iskey aggregates libevdev state over every /dev/input/event* and cannot
# say which device answered. On this device two nodes advertise KEY_VOLUMEUP:
# gpio_keys.16, which is the real volume key, and "Headset" (arizona-extcon),
# which synthesises headset remote key events and can emit one while the
# headphone detect pin is still settling early in boot. Nothing here advertises
# KEY_LEFTSHIFT, so the "hold left shift and volume up to fail the boot" check
# can only be triggered by volume-up, and a synthesised volume-up therefore
# halts the boot on its own. That is what killed boot 6: check_keys called
# fail_halt_boot at about 5.15s, before mount_subpartitions was reached, and
# with no framebuffer console there was nothing on screen to say why. s5iskey
# asks the same question but skips the device whose name says it synthesises
# keys, and prints which device did answer, so a log records the real source.
#
# Second, a single sample is not enough. A real keypress is held far longer than
# the three seconds this waits, so requiring persistence keeps the documented
# behaviour while ignoring momentary noise. This has still been observed to fire
# on one boot and not the next on the same image, so on a positive every input
# device name is also dumped: that is what separates "a person is holding volume
# up" from "a driver is stuck asserting it", and it is not recoverable after
# the fact. Set s5keys=n on the kernel command line to skip the check entirely.
# Usage: keys_still_held KEY [KEY...]
# Sets: (none)
# Returns: 0 if held on every sample, 1 if any sample disagreed
keys_still_held() {
\tlocal i out
\t[ $# -gt 0 ] || return 1
\t[ "$s5keys" = "n" ] && { echo "INFO: s5keys=$s5keys, ignoring held key $*"; return 1; }
\ti=1
\twhile [ "$i" -le 4 ]; do
\t\t# s5iskey, not iskey: it skips the synthesising headset device and
\t\t# names the device that answered. Its stdout is captured rather than
\t\t# left to the tee, so the device name is guaranteed to reach the log
\t\t# on the same path as every other line here.
\t\tif out="$(s5iskey --verbose "$@" 2>&1)"; then
\t\t\t[ -n "$out" ] && echo "$out"
\t\telse
\t\t\techo "INFO: keys $* not held on sample $i/4, continuing boot"
\t\t\treturn 1
\t\tfi
\t\t[ "$i" -lt 4 ] && sleep 1
\t\ti=$((i + 1))
\tdone
\techo "INFO: keys $* held on all 4 samples, treating as a real keypress"
\t# Which input devices exist at all. The H: capability bitmaps are 192 hex
\t# digits each and would bury the log for no gain, since s5iskey has already
\t# named the one that answered.
\tif [ -e /proc/bus/input/devices ]; then
\t\techo "INFO: input devices: $(grep -c '^N: ' /proc/bus/input/devices 2>/dev/null) total"
\t\tgrep '^N: ' /proc/bus/input/devices 2>/dev/null | sed 's/^/INFO:   /'
\telse
\t\techo "INFO: /proc/bus/input/devices is not readable"
\tfi
\techo "INFO: evdev nodes: $(ls /dev/input 2>/dev/null | tr '\\n' ' ')"
\treturn 0
}

# Bring up a USB serial console and give the installed system a login on it.
#
# This kernel has CONFIG_USB_F_ACM=y already, so no rebuild is needed. Both
# halves below were needed, and r15 shipped only the second one.
#
# 1. The gadget. USB networking can never bind a UDC on this device: ncm, rndis
#    and mass_storage are all absent from this kernel, so "Couldn't write new
#    UDC" during early init is the normal outcome and /dev/ttyGS0 is the only
#    way in. The shipped initramfs only ever creates the acm function from
#    debug_shell, and the teardown that removes it lives in that same function,
#    so on a successful boot the gadget was simply never created. r15 therefore
#    reached a login prompt on the framebuffer and offered no /dev/ttyGS0 at
#    all. setup_usb_acm_configfs also stops short of activating the gadget: it
#    clears UDC, creates the function and links it into the config, but does not
#    bind a controller, so setup_usb_configfs_udc has to be called as well.
#
# 2. The getty. OpenRC's init reads /etc/inittab, so one respawn line is all it
#    takes, and the getty path is probed rather than assumed because it is not
#    knowable from the initramfs which of /sbin, /bin or /usr/bin the target
#    system uses. Useless on its own without half 1, which is how r15 managed to
#    look correct and do nothing.
#
# Called from mount_root_partition(), so /sysroot is already mounted read-write
# and the installed system's own configuration can still be edited, and the
# gadget is up before switch_root. The gadget is kernel state and survives
# switch_root, so the tty the getty is pointed at is still there afterwards.
# Every step is logged, because whether any of this worked is only knowable from
# the log of the next boot.
ensure_usb_serial() {
\tlocal inittab=/sysroot/etc/inittab
\tlocal getty_bin="" cand line existing android_dev

\t# --- half 1: report what the early setup actually achieved ---
\t#
\t# The gadget is set up early, in setup_usb_network_configfs, because the
\t# controller can only carry one gadget at a time. All that is left to do
\t# here is to say whether that worked.
\t#
\t# Two earlier rounds each tested the wrong thing, and both tests passed on
\t# boots where the host had enumerated nothing at all.
\t#
\t# r16 tested for /dev/ttyGS0. This kernel's android_usb driver allocates
\t# every u_serial port at probe time, so that node exists whether or not a
\t# serial port is on the wire. The r16 boot printed
\t#
\t#     INFO: USB serial gadget is up, /dev/ttyGS0 exists
\t#
\t# with no USB device on the host.
\t#
\t# r17 tested for a link in configs/c.1. That is a real test, but of the
\t# wrong gadget stack: configfs never gets the controller on this device,
\t# so it correctly reported no link on a boot where android.c did hold the
\t# controller and was driving an ncm function.
\t#
\t# What is knowable from here is what was asked of the driver that owns the
\t# controller. Whether a host then enumerates anything is not knowable from
\t# the initramfs at all, and after r18 it is the only question left.
\tandroid_dev=/sys/class/android_usb/android0
\tif [ -e "$android_dev" ]; then
\t\techo "INFO: android_usb functions: [$(cat "$android_dev/functions" 2>/dev/null)]"
\t\techo "INFO: android_usb enable: [$(cat "$android_dev/enable" 2>/dev/null)]"
\telse
\t\techo "INFO: android_usb is not present on this kernel"
\t\tif [ -e "$CONFIGFS/g1/configs/c.1/$CONFIGFS_ACM_FUNCTION" ]; then
\t\t\techo "INFO: USB serial function is linked into the configuration"
\t\telse
\t\t\techo "INFO: USB serial function is NOT linked, there will be no serial port"
\t\tfi
\t\techo "INFO: usb gadget UDC: [$(cat "$CONFIGFS/g1/UDC" 2>/dev/null)]"
\t\techo "INFO: usb gadget functions: $(ls "$CONFIGFS/g1/functions" 2>/dev/null | tr '\\n' ' ')"
\t\techo "INFO: usb gadget config c.1: $(ls "$CONFIGFS/g1/configs/c.1" 2>/dev/null | tr '\\n' ' ')"
\tfi
\t# Printed only so the log carries both, and says out loud which one to
\t# ignore. Their existence is a probe-time artefact, not a result.
\techo "INFO: serial ports present: $(ls /dev/ttyGS* 2>/dev/null | tr '\\n' ' ')"
\techo "INFO: those exist on every boot and do not mean a host can see them"
\techo "INFO: controllers available: [$(ls /sys/class/udc 2>/dev/null | tr '\\n' ' ')]"

\t# --- half 2: the getty ---
\tif [ ! -e "$inittab" ]; then
\t\techo "INFO: $inittab does not exist, skipping the USB serial getty"
\t\treturn 0
\tfi
\tif grep -q 'ttyGS0' "$inittab" 2>/dev/null; then
\t\t# Log the line that is already there rather than only reporting that there
\t\t# is one. postmarketOS ships its own ttyGS0 entry, and on this install
\t\t# that is what all three r15 boots found, so the line appended further
\t\t# down never runs and the entry that will actually be used is pmOS's,
\t\t# not ours. Which binary it names, and whether that binary exists, is not
\t\t# knowable from the initramfs, and the installed root cannot be mounted
\t\t# from TWRP to find out: it is a nested MBR inside mmcblk0p21 exposed
\t\t# through device-mapper, and this recovery has no loop driver, no dmsetup,
\t\t# no modules to load and no shell. Printing it makes the next boot's log
\t\t# answer the question instead of costing another boot cycle.
\t\texisting="$(grep 'ttyGS0' "$inittab" 2>/dev/null)"
\t\techo "INFO: $inittab already has a ttyGS0 entry, leaving it untouched"
\t\t# A real inittab line carries no '=' and no credential, so printing it is
\t\t# safe. Anything that does is withheld rather than written to a log that
\t\t# gets copied off the device.
\t\tcase "$existing" in
\t\t\t*=*) echo "INFO: the existing ttyGS0 line carries options, not printing it" ;;
\t\t\t*) echo "INFO: the existing ttyGS0 line: $existing" ;;
\t\tesac
\t\t# Report whether the program it names is actually present. A shipped entry
\t\t# pointing at a missing binary is the one way this feature fails
\t\t# silently: the gadget comes up, /dev/ttyGS0 exists, and no getty is ever
\t\t# started on it.
\t\t#
\t\t# The program is field four of the line, so everything up to and
\t\t# including the third colon is dropped, and then its first word. Word
\t\t# splitting the whole line instead would yield "ttyGS0::respawn:/sbin/getty"
\t\t# as one token, in which the path is not a separate word at all and the
\t\t# test silently never matches anything.
\t\tcmd="${existing#*:*:*:}"
\t\tset -- $cmd
\t\tprog="$1"
\t\tif [ -z "$prog" ]; then
\t\t\techo "INFO: the existing ttyGS0 entry names no program to run"
\t\telse
\t\t\tcase "$prog" in
\t\t\t\t/*) cand="/sysroot$prog" ;;
\t\t\t\t*)
\t\t\t\t\t# A bare name would be resolved through PATH by the init, which
\t\t\t\t\t# the initramfs cannot see, so look where PATH would find it.
\t\t\t\t\tcand=""
\t\t\t\t\tfor d in /sysroot/sbin /sysroot/bin /sysroot/usr/bin /sysroot/usr/sbin; do
\t\t\t\t\t\tif [ -e "$d/$prog" ]; then cand="$d/$prog"; break; fi
\t\t\t\t\tdone
\t\t\t\t\t;;
\t\t\tesac
\t\t\tif [ -z "$cand" ]; then
\t\t\t\techo "INFO: the existing entry's $prog was not found in any bin dir"
\t\t\telif [ -e "$cand" ]; then
\t\t\t\techo "INFO: the existing entry's $prog exists at $cand"
\t\t\telse
\t\t\t\techo "INFO: the existing entry's $prog does NOT exist, so no serial login"
\t\t\tfi
\t\tfi
\t\treturn 0
\tfi
\tfor cand in /sysroot/sbin/getty /sysroot/bin/getty /sysroot/usr/bin/getty; do
\t\tif [ -e "$cand" ]; then
\t\t\tgetty_bin="${cand#/sysroot}"
\t\t\tbreak
\t\tfi
\tdone
\tif [ -z "$getty_bin" ]; then
\t\techo "INFO: no getty binary found under /sysroot, skipping the USB serial getty"
\t\treturn 0
\tfi
\t# -L keeps getty local rather than waiting on a carrier detect, which a
\t# USB gadget serial port never asserts. The vt100 terminal type is what the
\t# other getty lines in this file already use.
\tline="ttyGS0::respawn:$getty_bin -L ttyGS0 vt100"
\tif printf '%s\\n' "$line" >> "$inittab" 2>/dev/null; then
\t\techo "INFO: added '$line' to $inittab"
\telse
\t\techo "INFO: could not append to $inittab, there will be no USB serial login"
\tfi
}
# <<< s5screen: helpers (end)
'''


def sub(text, old, new, what):
    if text.count(old) != 1:
        raise SystemExit(f"{what}: expected exactly one match, found {text.count(old)}")
    return text.replace(old, new)


def main():
    path = Path(sys.argv[1])
    t = path.read_text()
    n = len(t)

    # 1. helper functions, immediately before mount_subpartitions()
    t = sub(
        t,
        "# <<< dm-bypass: helpers (end)\nmount_subpartitions() {\n",
        "# <<< dm-bypass: helpers (end)\n" + HELPERS + "mount_subpartitions() {\n",
        "helpers",
    )

    # 2. "we are looking for the root now"
    t = sub(
        t,
        '\techo "Trying to mount subpartitions for $wait_seconds seconds..."\n',
        '\techo "Trying to mount subpartitions for $wait_seconds seconds..."\n'
        "\t# >>> s5screen: booting hook (begin)\n"
        "\ts5status booting\n"
        "\t# <<< s5screen: booting hook (end)\n",
        "booting hook",
    )

    # 3. device-mapper outcome, inside the existing dm-bypass region
    t = sub(
        t,
        "\t\t\t\tif dm_attach_subpartitions \"$partition\"; then\n"
        "\t\t\t\t\tSUBPARTITION_LOOP=\"\"\n"
        "\t\t\t\t\tbreak\n"
        "\t\t\t\tfi\n",
        "\t\t\t\tif dm_attach_subpartitions \"$partition\"; then\n"
        "\t\t\t\t\tSUBPARTITION_LOOP=\"\"\n"
        "\t\t\t\t\t# >>> s5screen: dmok hook (begin)\n"
        "\t\t\t\t\ts5status dmok\n"
        "\t\t\t\t\t# <<< s5screen: dmok hook (end)\n"
        "\t\t\t\t\tbreak\n"
        "\t\t\t\tfi\n"
        "\t\t\t\t# >>> s5screen: dmno hook (begin)\n"
        "\t\t\t\ts5status dmno\n"
        "\t\t\t\t# <<< s5screen: dmno hook (end)\n",
        "dmok/dmno hooks",
    )

    # 4. subpartitions gave up entirely
    t = sub(
        t,
        '\t\t\techo "ERROR: failed to mount subpartitions!"\n\t\t\treturn;\n',
        '\t\t\techo "ERROR: failed to mount subpartitions!"\n'
        "\t\t\t# >>> s5screen: loopfail hook (begin)\n"
        "\t\t\ts5status loopfail\n"
        "\t\t\t# <<< s5screen: loopfail hook (end)\n"
        "\t\t\treturn;\n",
        "loopfail hook",
    )

    # 5. root filesystem is mounted and looks like a real system: we are done.
    #    The blank line has to live inside the region, otherwise stripping
    #    would leave it behind and the file would no longer equal the base.
    t = sub(
        t,
        '\tif ! [ -e /sysroot/etc/os-release ]; then\n'
        '\t\tsplash_set_error "Root partition does not contain a root filesystem'
        '\\nhttps://postmarketos.org/troubleshooting"\n'
        "\t\tfail_halt_boot\n"
        "\tfi\n"
        "}\n",
        '\tif ! [ -e /sysroot/etc/os-release ]; then\n'
        '\t\tsplash_set_error "Root partition does not contain a root filesystem'
        '\\nhttps://postmarketos.org/troubleshooting"\n'
        "\t\tfail_halt_boot\n"
        "\tfi\n"
        "\t# >>> s5screen: booted hook (begin)\n"
        "\n"
        "\ts5status booted\n"
        "\t# Root is mounted read-write here, so this is the first point at which\n"
        "\t# the USB serial gadget can be activated and the installed system can\n"
        "\t# be given a login prompt on it.\n"
        "\tensure_usb_serial\n"
        "\t# <<< s5screen: booted hook (end)\n"
        "}\n",
        "booted hook",
    )

    # 6. Put a serial function on the wire, through whichever gadget stack
    #    actually owns the controller.
    #
    #    This is a pure insertion: not one byte of the shipped function is
    #    altered, so stripping the region reproduces the base exactly. It has to
    #    be an insertion rather than a rewrite because the whole scheme rests on
    #    the delta being provable by strip-back.
    #
    #    Two rounds got the position right and the mechanism wrong.
    #
    #    r16 created acm.usb0 from mount_root_partition, by which time the
    #    network path had already bound the UDC to a configuration with no
    #    functions in it, and the log from that boot shows what the kernel does
    #    with that:
    #
    #        ash: write error: No such device
    #        mkdir: .../functions/acm.usb0: Resource busy
    #        ln: .../configs/c.1/acm.usb0: No such file or directory
    #
    #    r17 moved the call to the last moment before the bind, which is the
    #    only point at which the configuration is still inactive, and it still
    #    failed, with the same EBUSY, for the same reason as r16's second
    #    error. Being early was necessary and not sufficient, because the bind
    #    that r17 was trying to precede had already happened somewhere else.
    #
    #    The controller is bound by android.c, not by configfs, and the region
    #    below works with that instead of against it.
    t = sub(
        t,
        "\t# If an argument was supplied then skip writing to the UDC (only used"
        " for mass storage\n\t# log recovery)\n",
        "\t# >>> s5screen: usb serial function (begin)\n"
        "\t# Put a USB serial function on the wire.\n"
        "\t#\n"
        "\t# r17 created acm.usb0 through configfs, at the right position,\n"
        "\t# before any bind, and the kernel still refused it with EBUSY. The\n"
        "\t# reason is that configfs is not the gadget stack driving this\n"
        "\t# controller. pmOS's own setup_usb_network_android runs first, hands\n"
        "\t# the controller to drivers/usb/gadget/android.c, and that driver\n"
        "\t# has already bound it. From the r17 boot log:\n"
        "\t#\n"
        "\t#     usb: functions_store buff=ncm\n"
        "\t#     usb: enable_store enabled=1\n"
        "\t#     usb: android_bind_enabled_functions f:ncm\n"
        "\t#     ncm0: MAC 8a:5d:d5:50:cf:d3\n"
        "\t#\n"
        "\t# Two separate failures follow from that one fact. The configfs UDC\n"
        "\t# store returns EBUSY, because that file's only EBUSY is the\n"
        "\t# gi->udc_name-already-set case. And acm.usb0 cannot be created,\n"
        "\t# because android.c runs f->init for every function in its table at\n"
        "\t# probe time, so acm_function_init had already called\n"
        "\t# gserial_alloc_line MAX_ACM_INSTANCES (4) times and taken all\n"
        "\t# MAX_U_SERIAL_PORTS (4) ports. gserial_alloc_line walks past busy\n"
        "\t# ports, so it only hands back EBUSY once the last one is gone.\n"
        "\t#\n"
        "\t# So the fix is not to try harder with configfs, it is to stop\n"
        "\t# competing for the controller and ask the driver that owns it.\n"
        "\t# CONFIG_USB_F_ACM=y is already in this kernel, which is also why\n"
        "\t# /dev/ttyGS0 exists even on a boot with nothing on the wire: the\n"
        "\t# probe-time port allocation is unconditional.\n"
        "\tandroid_dev=/sys/class/android_usb/android0\n"
        "\tif [ -e \"$android_dev\" ]; then\n"
        "\t\techo \"INFO: android_usb owns the controller, asking it for a serial function\"\n"
        "\t\t# Disable first, so the ncm function that is currently bound is\n"
        "\t\t# released and the descriptor is replaced rather than added to.\n"
        "\t\techo \"0\" > \"$android_dev/enable\" 2>/dev/null \\\n"
        "\t\t\t|| echo \"INFO: could not disable android_usb\"\n"
        "\t\techo \"INFO: android_usb was set to: [$(cat \"$android_dev/functions\" 2>/dev/null)]\"\n"
        "\t\t# pmOS asks for ncm, which a Mac host does not turn into anything\n"
        "\t\t# usable. acm is the CDC-ACM function, the one a host can open a\n"
        "\t\t# serial port on.\n"
        "\t\tif echo acm > \"$android_dev/functions\" 2>/dev/null; then\n"
        "\t\t\techo \"INFO: asked android_usb for the acm function\"\n"
        "\t\telse\n"
        "\t\t\techo \"INFO: android_usb refused the acm function\"\n"
        "\t\tfi\n"
        "\t\techo \"1\" > \"$android_dev/enable\" 2>/dev/null \\\n"
        "\t\t\t|| echo \"INFO: could not re-enable android_usb\"\n"
        "\t\techo \"INFO: android_usb is now: [$(cat \"$android_dev/functions\" 2>/dev/null)]\"\n"
        "\telif [ -e \"$CONFIGFS\" ]; then\n"
        "\t\t# Only reached on a kernel with no android_usb driver at all, and\n"
        "\t\t# then configfs is the only gadget stack there is.\n"
        "\t\tmkdir -p \"$CONFIGFS/g1/functions/$CONFIGFS_ACM_FUNCTION\" \\\n"
        "\t\t\t|| echo \"INFO: could not create $CONFIGFS_ACM_FUNCTION\"\n"
        "\t\tmkdir -p \"$CONFIGFS/g1/configs/c.1\" \\\n"
        "\t\t\t|| echo \"INFO: could not create configs/c.1\"\n"
        "\t\tln -sf \"$CONFIGFS/g1/functions/$CONFIGFS_ACM_FUNCTION\" \\\n"
        "\t\t\t\"$CONFIGFS/g1/configs/c.1\" \\\n"
        "\t\t\t|| echo \"INFO: could not link $CONFIGFS_ACM_FUNCTION into c.1\"\n"
        "\t\techo \"INFO: usb gadget functions: $(ls \"$CONFIGFS/g1/functions\" 2>/dev/null | tr '\\n' ' ')\"\n"
        "\t\techo \"INFO: usb gadget config c.1: $(ls \"$CONFIGFS/g1/configs/c.1\" 2>/dev/null | tr '\\n' ' ')\"\n"
        "\telse\n"
        "\t\techo \"INFO: neither android_usb nor $CONFIGFS is present, no USB serial gadget\"\n"
        "\tfi\n"
        "\t# Two dwc3 controllers register gadgets on this device, so name them\n"
        "\t# while they can still be listed. If the serial port is still not on\n"
        "\t# the wire after this, the controller the driver chose is the next\n"
        "\t# thing to look at, and this line is what says which ones exist.\n"
        "\techo \"INFO: controllers available: [$(ls /sys/class/udc 2>/dev/null | tr '\\n' ' ')]\"\n"
        "\t# <<< s5screen: usb serial function (end)\n"
        "\t# If an argument was supplied then skip writing to the UDC (only used"
        " for mass storage\n\t# log recovery)\n",
        "usb serial function",
    )

    # 7. do not act on a single iskey sample.
    #
    #    The original "fail_halt_boot" line is left byte for byte where it was
    #    and simply wrapped by the added if, rather than being replaced. That
    #    keeps the change a pure line insertion, so stripping the two regions
    #    reproduces the base file exactly and the delta stays provable. The
    #    price is one level of stale indentation on that line, which is the
    #    trade this whole scheme is built on.
    t = sub(
        t,
        "\t\telif iskey KEY_LEFTSHIFT KEY_VOLUMEUP; then\n"
        "\t\t\tfail_halt_boot\n"
        "\t\tfi\n",
        "\t\telif iskey KEY_LEFTSHIFT KEY_VOLUMEUP; then\n"
        "\t\t\t# >>> s5screen: check_keys guard (begin)\n"
        "\t\t\t# iskey is not trusted on a single sample: see keys_still_held().\n"
        "\t\t\tif keys_still_held KEY_LEFTSHIFT KEY_VOLUMEUP; then s5status keys\n"
        "\t\t\t# <<< s5screen: check_keys guard (end)\n"
        "\t\t\tfail_halt_boot\n"
        "\t\t\t# >>> s5screen: check_keys guard tail (begin)\n"
        "\t\t\tfi\n"
        "\t\t\t# <<< s5screen: check_keys guard tail (end)\n"
        "\t\tfi\n",
        "check_keys guard",
    )

    path.write_text(t)
    print(f"{path}: {n} -> {len(t)} bytes (+{len(t) - n})")


if __name__ == "__main__":
    main()
