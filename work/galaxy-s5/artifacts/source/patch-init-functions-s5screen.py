#!/usr/bin/env python3
"""Insert the s5screen status display and the check_keys hardening.

Applied to the shipped DM-bypass init_functions.sh. Every change lives inside a
"# >>> s5screen: ... (begin)" / "# <<< s5screen: ... (end)" pair (plus the two
pre-existing dm-bypass pairs) so repack-boot-s5screen.py can strip them all and
assert the remainder is byte-for-byte the base file. If this script's edits ever
drift outside a marked region, that assertion fails instead of silently shipping.
"""

import sys
from pathlib import Path

HELPERS = '''# >>> s5screen: helpers (begin)
# Paint the boot state onto the panel, and stop trusting a single iskey sample.
# Both are diagnostic-only and must never be able to stop a boot.
#
# s5status <state> shows one of: booting, dmok, dmno, loopfail, keys, booted.
# This kernel sets CONFIG_VT_CONSOLE=y but leaves CONFIG_FRAMEBUFFER_CONSOLE
# unset, and the stage-1 CPIO contains no plymouth at all, so the panel stays
# black for the whole first stage. Without this, a boot that fails early looks
# exactly like a boot that is merely quiet, which is what made boots 5 and 6 so
# hard to read. s5screen mmaps /dev/fb0 and paints the state itself: the
# framebuffer is the only output available, and on this device it has to be
# written through mmap, because the fbdev accepts a plain write(2) but consumes
# nothing, and rejects msync(2) with EINVAL. Every failure inside s5screen is
# swallowed and it always exits 0, so this can cost the screen but never a boot.
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
# samples a second apart before believing that anybody is pressing them.
#
# iskey aggregates libevdev state over every /dev/input/event*, and on this
# device two of those advertise KEY_VOLUMEUP: gpio_keys, which is the real
# volume key, and "Headset" (arizona-extcon), which synthesises headset remote
# key events and can emit one while the headphone detect pin is still settling
# early in boot. Nothing here advertises KEY_LEFTSHIFT, so a false positive can
# only be KEY_VOLUMEUP. One spurious sample from either device is enough to make
# iskey claim a key is held, and that is exactly what killed boot 6: check_keys
# called fail_halt_boot at about 5.15s, before mount_subpartitions was ever
# reached, leaving a black screen and no other clue. A real keypress is held far
# longer than the three seconds this waits, so requiring persistence keeps the
# documented "hold left shift and volume up to fail the boot" behaviour while
# ignoring momentary noise. Set s5keys=n on the kernel command line to skip the
# check entirely.
#
# Persistence alone is not enough to be sure, because the four samples are only
# three seconds apart and this has already been observed to fire on one boot
# and not the next on the same image. So on a positive, every input device is
# also dumped: /proc/bus/input/devices names the device that reported the key
# and lists its capabilities, which is what actually distinguishes "a person is
# holding volume up" from "gpio_keys or arizona-extcon is stuck asserting it".
# Usage: keys_still_held KEY [KEY...]
# Sets: (none)
# Returns: 0 if held on every sample, 1 if any sample disagreed
keys_still_held() {
\tlocal i key
\t[ $# -gt 0 ] || return 1
\t[ "$s5keys" = "n" ] && { echo "INFO: s5keys=$s5keys, ignoring held key $*"; return 1; }
\ti=1
\twhile [ "$i" -le 4 ]; do
\t\tif ! iskey "$@"; then
\t\t\techo "INFO: keys $* not held on sample $i/4, continuing boot"
\t\t\treturn 1
\t\tfi
\t\t# iskey succeeds when *any* of the keys is down, so name the ones
\t\t# that actually are: otherwise a report of which key stopped the
\t\t# boot is impossible to tell apart from a guess.
\t\tfor key in "$@"; do
\t\t\tiskey "$key" && echo "INFO: sample $i/4, $key is held"
\t\tdone
\t\t[ "$i" -lt 4 ] && sleep 1
\t\ti=$((i + 1))
\tdone
\techo "INFO: keys $* held on all 4 samples, treating as a real keypress"
\t# Which input devices exist and what each can report. Only gpio_keys and
\t# Headset (arizona-extcon) advertise KEY_VOLUMEUP on this hardware, so
\t# which of them is present, and what the rest of the input stack looked
\t# like at the moment, is what separates a person holding volume up from a
\t# driver stuck asserting it. Not recoverable after the fact.
\tif [ -e /proc/bus/input/devices ]; then
\t\t# Names only: the H: capability bitmaps are 192 hex digits each and
\t\t# would bury the log for no gain, since the check above already ran.
\t\techo "INFO: input devices: $(grep -c '^N: ' /proc/bus/input/devices 2>/dev/null) total"
\t\tgrep '^N: ' /proc/bus/input/devices 2>/dev/null | sed 's/^/INFO:   /'
\telse
\t\techo "INFO: /proc/bus/input/devices is not readable"
\tfi
\techo "INFO: evdev nodes: $(ls /dev/input 2>/dev/null | tr '\\n' ' ')"
\treturn 0
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
        "\t# <<< s5screen: booted hook (end)\n"
        "}\n",
        "booted hook",
    )

    # 6. do not act on a single iskey sample.
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
