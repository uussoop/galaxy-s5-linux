#!/bin/sh
# Second read-only probe: validate the exact parsing and device-mapper table
# the initramfs bypass will use, with the initramfs' own userland.
#
# Part A parses the nested MBR exactly like the patched init_functions.sh will.
# Part B creates one temporary read-only device-mapper linear table under a
# throwaway name, inspects it, and removes it again. The real
# mmcblk0p21p1/p2 mappings stay untouched, no filesystem is mounted, and no
# data is written to any partition.

set -u

C=/tmp/codex-s5-maint/chroot
D=/tmp/codex-s5-probe
P=/dev/block/mmcblk0p21
LOADER="$C/lib/ld-musl-armhf.so.1"
NAME=s5probe

die() { echo "STOP: $*" >&2; exit 1; }

[ -x "$LOADER" ] || die "staged musl loader missing"
[ -b "$P" ] || die "physical USERDATA p21 absent"
run() { "$LOADER" --library-path "$C/lib" "$D/busybox" "$@"; }
dm() { "$LOADER" --library-path "$C/lib:$D/lib" "$D/dmsetup" "$@"; }

echo "=== A. MBR parsing contract (identical to the patched code) ==="
base=$(basename "$P")
# The real contract, taken from the observed busybox output:
#   $1 = device path with the pN suffix, $NF = "Linux", $(NF-1) = "83"
entries=$(run fdisk -l "$P" 2>/dev/null | run awk -v dev="$P" '
	NF >= 8 && $1 ~ "^" dev "p[0-9]+$" && $NF == "Linux" && $(NF-1) == "83" &&
	$(NF-2) ~ /^[0-9]+(\.[0-9]+)?[KMGTP]?$/ && $(NF-3) ~ /^[0-9]+$/ &&
	$(NF-4) ~ /^[0-9]+$/ && $(NF-5) ~ /^[0-9]+$/ {
		printf "%s p%s %s %s\n", dev, substr($1, length(dev) + 2), $(NF-5), $(NF-3)
	}
')
echo "$entries"
count=$(echo "$entries" | run grep -c .)
echo "entry count: $count"
[ "$count" -eq 2 ] || die "guard would reject: expected exactly 2 entries"

part_sectors=$(cat /sys/class/block/"$base"/size)
ss=$(run blockdev --getss "$P" 2>/dev/null)
echo "partition sectors: $part_sectors, blockdev --getss: '$ss'"
ss2=$(cat /sys/class/block/"$base"/queue/logical_block_size 2>/dev/null)
echo "sysfs logical_block_size: '$ss2'"

echo "--- validate each entry against the recorded layout ---"
echo "$entries" | run awk -v psize="$part_sectors" '
	{
		if (prev_seen && $3 < prev_end) { print "REJECT not in ascending order: " $2; bad = 1 }
		if ($3 < 2048) { print "REJECT start<2048 for " $2; bad = 1 }
		if ($4 < 1) { print "REJECT empty for " $2; bad = 1 }
		if ($3 + $4 > psize) { print "REJECT outside parent for " $2; bad = 1 }
		if ($3 % 2048 != 0) { print "NOTE unaligned start " $3 " for " $2 }
		prev_end = $3 + $4
		prev_seen = 1
	}
	END { exit bad ? 1 : 0 }' || die "entry validation failed"
echo "entries validated"

echo "=== B. temporary device-mapper linear table ==="
dm info -c --noheadings -o name 2>/dev/null | grep -qx "$NAME" && die "$NAME already exists"
table0="0 497664 linear $P 2048"
table1="0 24346624 linear $P 499712"
echo "table p1: $table0"
echo "table p2: $table1"
dm create "$NAME"1 --table "$table0" || die "dmsetup create failed"
dm create "$NAME"2 --table "$table1" || die "dmsetup create failed"
dm mknodes 2>&1
echo "--- dmsetup table ---"
dm table "$NAME"1
dm table "$NAME"2
echo "--- nodes ---"
ls -la /dev/mapper
echo "--- sysfs identity ---"
for n in "$NAME"1 "$NAME"2; do
	dev=$(readlink -f "/sys/class/block/mapper/$n" 2>/dev/null)
	[ -n "$dev" ] || dev=$(readlink -f "/sys/block/$n" 2>/dev/null)
	echo "$n -> $dev size=$(cat "$dev/size" 2>/dev/null) slaves=$(ls "$dev/slaves" 2>/dev/null)"
	echo "   block node: $(run stat -c '%t:%T' "/dev/mapper/$n" 2>/dev/null)"
	echo "   blkid: $(run blkid "/dev/mapper/$n" 2>&1)"
	echo "   read: $(run dd if="/dev/mapper/$n" bs=1024 count=1 2>/dev/null | run wc -c)"
done
echo "--- blkid scan by label (what find_partition does) ---"
echo "root by label: $(run blkid --label pmOS_root 2>&1)"
echo "boot by label: $(run blkid --label pmOS_boot 2>&1)"

echo "--- teardown ---"
dm remove "$NAME"2 && dm remove "$NAME"1
rm -f "/dev/mapper/$NAME"1 "/dev/mapper/$NAME"2
echo "remaining tables:"; dm ls --tree -c 2>&1
echo "PROBE DONE"
