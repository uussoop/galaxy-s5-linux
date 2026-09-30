#!/bin/sh
# Third read-only probe: why does `blkid --label` miss the device-mapper
# partitions? This decides whether the bypass may rely on blkid's search or
# has to pin the partition paths itself.
#
# Only temporary in-memory device-mapper tables are created; no filesystem is
# mounted and no partition is written.

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

echo "=== /proc/partitions (which majors does dm use here?) ==="
run cat /proc/partitions
echo "=== mmcblk0p21 major ==="
run stat -c '%t:%T' "$P"
echo "=== dm device majors ==="
run stat -c '%t:%T' /dev/mapper/mmcblk0p21p1 /dev/mapper/mmcblk0p21p2
echo "=== util-linux blkid version ==="
"$LOADER" --library-path "$C/lib" /tmp/s5-diag-rd/usr/bin/blkid --version 2>&1 || \
	run blkid --help 2>&1 | run head -2

echo "=== create temporary tables again ==="
dm create "$NAME"1 --table "0 497664 linear $P 2048" || die "create failed"
dm create "$NAME"2 --table "0 24346624 linear $P 499712" || die "create failed"
dm mknodes 2>&1

root_id=$(run blkid /dev/mapper/"$NAME"2)
boot_id=$(run blkid /dev/mapper/"$NAME"1)
root_uuid=$(echo "$root_id" | run tr ' ' '\n' | run grep '^UUID=' | run cut -d'"' -f2)
echo "direct probe root: $root_id"
echo "direct probe boot: $boot_id"
echo "root uuid: $root_uuid"

echo "=== blkid lookups (the ones find_partition uses) ==="
echo "  blkid --label pmOS_root   -> '$(run blkid --label pmOS_root 2>&1)'"
echo "  blkid --label pmOS_boot   -> '$(run blkid --label pmOS_boot 2>&1)'"
if [ -n "$root_uuid" ]; then
	echo "  blkid --uuid $root_uuid -> '$(run blkid --uuid "$root_uuid" 2>&1)'"
fi
echo "  blkid -L pmOS_root        -> '$(run blkid -L pmOS_root 2>&1)'"
echo "  blkid -i -L pmOS_root     -> '$(run blkid -i -L pmOS_root 2>&1)'"
echo "  blkid -t LABEL=pmOS_root  -> '$(run blkid -t LABEL=pmOS_root 2>&1)'"
echo "  blkid -l                  -> '$(run blkid -l 2>&1 | run head -3)'"
echo "  blkid /dev/mapper/$NAME 2 -> '$(run blkid /dev/mapper/"$NAME"2 2>&1)'"

echo "=== teardown ==="
dm remove "$NAME"2 && dm remove "$NAME"1
rm -f "/dev/mapper/$NAME"1 "/dev/mapper/$NAME"2
dm ls --tree -c 2>&1
echo "PROBE DONE"
