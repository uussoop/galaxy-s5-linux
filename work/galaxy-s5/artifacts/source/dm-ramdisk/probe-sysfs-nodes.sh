#!/bin/sh
# Fifth read-only probe: confirm the exact sysfs attributes and node handling
# the bypass relies on, plus that the applets it needs are present.
# Temporary in-memory device-mapper tables only; nothing is mounted or written.

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

echo "=== applets the bypass uses ==="
run --list | run grep -xE "blockdev|mknod|awk|fdisk|dirname|readlink|cat|grep" | run sort | run tr '\n' ' '
echo
echo "=== blockdev --getss on the real partition ==="
run blockdev --getss "$P" 2>&1

echo "=== create temporary tables ==="
dm create "$NAME"1 --table "0 497664 linear $P 2048" || die "create failed"
dm create "$NAME"2 --table "0 24346624 linear $P 499712" || die "create failed"
dm mknodes 2>&1

echo "=== /sys/class/block/mapper/<name> attributes (guard inputs) ==="
for n in "$NAME"1 "$NAME"2; do
	echo "--- $n"
	echo "  size:  $(cat /sys/class/block/mapper/$n/size 2>&1)"
	echo "  dev:   $(cat /sys/class/block/mapper/$n/dev 2>&1)"
	echo "  dm/name: $(cat /sys/class/block/mapper/$n/dm/name 2>&1)"
	echo "  slaves: $(ls /sys/class/block/mapper/$n/slaves 2>&1 | run tr '\n' ' ')"
	echo "  node:  $(ls -la /dev/mapper/$n 2>&1 | run tr -s ' ' '_')"
done
echo "  p21 partition sysfs: $(cat /sys/class/block/mmcblk0p21/partition 2>&1)"
echo "  p21 sysfs path: $(readlink -f /sys/class/block/mmcblk0p21 2>&1)"

echo "=== node fallback: remove the node and rebuild it from sysfs ==="
rm -f "/dev/mapper/$NAME"1
[ -e "/dev/mapper/$NAME"1 ] && die "could not remove test node"
devno=$(cat /sys/class/block/mapper/"$NAME"1/dev)
maj=${devno%:*}; min=${devno#*:}
echo "  sysfs dev: $devno ($maj:$min)"
run mknod "/dev/mapper/$NAME"1 b "$((0x$maj))" "$((0x$min))" 2>&1 || die "mknod failed"
ls -la "/dev/mapper/$NAME"1
echo "  blkid after mknod: $(run blkid "/dev/mapper/$NAME"1 2>&1)"
echo "  read after mknod: $(run dd if="/dev/mapper/$NAME"1 bs=1024 count=1 2>/dev/null | run wc -c)"

echo "=== teardown ==="
dm remove "$NAME"2 && dm remove "$NAME"1
rm -f "/dev/mapper/$NAME"1 "/dev/mapper/$NAME"2
dm ls --tree -c 2>&1
echo "PROBE DONE"
