#!/bin/sh
# Sixth read-only probe: locate device-mapper devices in sysfs the way this
# kernel actually exposes them, so the bypass can verify each mapping before
# trusting it, and rebuild /dev/mapper/<name> nodes itself if needed.
# Temporary in-memory tables only; nothing is mounted or written.

set -u

C=/tmp/codex-s5-maint/chroot
D=/tmp/codex-s5-probe
P=/dev/block/mmcblk0p21
LOADER="$C/lib/ld-musl-armhf.so.1"
NAME=s5probe

die() { echo "STOP: $*" >&2; exit 1; }
run() { "$LOADER" --library-path "$C/lib" "$D/busybox" "$@"; }
dm() { "$LOADER" --library-path "$C/lib:$D/lib" "$D/dmsetup" "$@"; }

[ -x "$LOADER" ] || die "staged musl loader missing"
[ -b "$P" ] || die "physical USERDATA p21 absent"

echo "=== clean up leftovers from the previous probe ==="
for n in $(dm info -c --noheadings -o name 2>/dev/null | run grep "^$NAME" || true); do
	echo "  removing $n"
	dm remove "$n" 2>&1 || true
	rm -f "/dev/mapper/$n"
done
dm ls --tree -c 2>&1

echo "=== create temporary tables ==="
dm create "$NAME"1 --table "0 497664 linear $P 2048" || die "create failed"
dm create "$NAME"2 --table "0 24346624 linear $P 499712" || die "create failed"
dm mknodes 2>&1

echo "=== how the kernel exposes dm devices in sysfs ==="
echo "--- /sys/class/block entries matching dm"
run ls -la /sys/class/block/ | run grep -E "dm-|mapper" || echo "(none)"
echo "--- dmsetup info -c"
for n in "$NAME"1 "$NAME"2; do
	echo "  $n: major=$(dm info -c --noheadings -o major "$n") minor=$(dm info -c --noheadings -o minor "$n")"
done
echo "--- /sys/dev/block resolution (what the maintenance README uses)"
for n in "$NAME"1 "$NAME"2; do
	maj=$(dm info -c --noheadings -o major "$n")
	min=$(dm info -c --noheadings -o minor "$n")
	sys=$(readlink -f "/sys/dev/block/$maj:$min")
	echo "  $n -> $sys"
	echo "     name:   $(cat "$sys/dm/name" 2>&1)"
	echo "     size:   $(cat "$sys/size" 2>&1)"
	echo "     slaves: $(ls "$sys/slaves" 2>&1 | run tr '\n' ' ')"
	echo "     class:  $(readlink -f /sys/class/block/"$(basename "$sys")" 2>&1)"
done

echo "=== node rebuild from dmsetup's major:minor ==="
rm -f "/dev/mapper/$NAME"1
maj=$(dm info -c --noheadings -o major "$NAME"1)
min=$(dm info -c --noheadings -o minor "$NAME"1)
run mknod "/dev/mapper/$NAME"1 b "$maj" "$min" || die "mknod failed"
ls -la "/dev/mapper/$NAME"1
echo "  blkid: $(run blkid "/dev/mapper/$NAME"1 2>&1)"
echo "  read:  $(run dd if="/dev/mapper/$NAME"1 bs=1024 count=1 2>/dev/null | run wc -c)"

echo "=== teardown ==="
dm remove "$NAME"2 && dm remove "$NAME"1
rm -f "/dev/mapper/$NAME"1 "/dev/mapper/$NAME"2
dm ls --tree -c 2>&1
echo "PROBE DONE"
