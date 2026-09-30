#!/bin/sh
# Functional test of the device-mapper bypass from the patched
# init_functions.sh, run against the real nested USERDATA partitions under
# TWRP.
#
# The functions are taken verbatim from the patched file, with exactly one
# change: dm_attach_one() names its mappings "s5t<base>p<n>" instead of
# "<base>p<n>", because kpartx already owns the mmcblk0p21p1/mmcblk0p21p2
# names for the maintenance mounts. Everything else, including every guard, is
# the code that will run in the initramfs.
#
# TWRP's shell has no awk and cannot run the pushed musl binaries by shebang,
# so the applets the patched code uses are provided as wrapper functions
# around the *initramfs' own* binaries in /tmp/codex-s5-probe. The tested
# userland is therefore the same one that will run on the phone.
#
# Read-only with respect to stored data: mappings live in kernel memory,
# nothing is mounted, and every mapping is removed again at the end.

set -u

C=/tmp/codex-s5-maint/chroot
D=/tmp/codex-s5-probe
LOADER="$C/lib/ld-musl-armhf.so.1"
P=/dev/block/mmcblk0p21
TESTFILE=/tmp/codex-s5-probe/init_functions.test.sh

die() { echo "STOP: $*" >&2; exit 1; }
fail() { echo "FAIL: $*" >&2; exit 1; }

[ -x "$LOADER" ] || die "staged musl loader missing"
[ -b "$P" ] || die "physical USERDATA p21 absent"
[ -r "$TESTFILE" ] || die "test copy of init_functions.sh missing"

# The initramfs userland, reachable from TWRP's shell.
bb() { "$LOADER" --library-path "$C/lib" "$D/busybox" "$@"; }
dmsetup() { "$LOADER" --library-path "$C/lib:$D/lib" "$D/dmsetup" "$@"; }
blkid() { "$LOADER" --library-path "$C/lib:$D/lib" "$D/blkid" "$@"; }
busybox() { "$LOADER" --library-path "$C/lib" "$D/busybox" "$@"; }
for a in awk blockdev fdisk mknod mkdir readlink cat grep sed tr wc ls rm dd stat; do
	eval "$a() { bb $a \"\$@\"; }"
done

# shellcheck source=/dev/null
. "$TESTFILE"

echo "=== state before ==="
dmsetup ls --tree -c 2>&1

echo
echo "=== 1. dm_control_node() rebuilds the control node the initramfs lacks ==="
echo "  before: $(ls -la /dev/mapper/control 2>&1)"
mv /dev/mapper/control /dev/mapper/control.saved || fail "could not move the control node"
if ! dm_control_node; then
	mv /dev/mapper/control.saved /dev/mapper/control
	fail "dm_control_node did not recreate the control node"
fi
echo "  after:  $(ls -la /dev/mapper/control 2>&1)"
echo "  /proc/misc: $(grep device-mapper /proc/misc)"
echo "  device-mapper usable again: $(dmsetup table mmcblk0p21p1 2>&1)"
rm -f /dev/mapper/control
mv /dev/mapper/control.saved /dev/mapper/control || fail "could not restore the control node"
echo "  restored: $(ls -la /dev/mapper/control 2>&1)"

echo
echo "=== 2. the bypass maps the two nested partitions and pins them ==="
PMOS_BOOT=""
PMOS_ROOT=""
if ! dm_attach_subpartitions "$P"; then
	fail "dm_attach_subpartitions refused the real nested partitions"
fi
echo "PMOS_BOOT=$PMOS_BOOT"
echo "PMOS_ROOT=$PMOS_ROOT"
[ "$PMOS_BOOT" = "/dev/mapper/s5tmmcblk0p21p1" ] || fail "unexpected PMOS_BOOT"
[ "$PMOS_ROOT" = "/dev/mapper/s5tmmcblk0p21p2" ] || fail "unexpected PMOS_ROOT"
echo "created mappings:$DM_SUBPARTITIONS"
echo "--- tables (must mirror the kpartx tables) ---"
dmsetup table s5tmmcblk0p21p1
dmsetup table s5tmmcblk0p21p2
echo "--- identity ---"
for n in s5tmmcblk0p21p1 s5tmmcblk0p21p2; do
	sys=$(readlink -f "/sys/dev/block/$(dmsetup info -c --noheadings -o major "$n"):$(dmsetup info -c --noheadings -o minor "$n")")
	echo "  /dev/mapper/$n: $(blkid "/dev/mapper/$n" 2>&1)"
	echo "    sysfs: $sys"
	echo "    slaves: $(ls "$sys/slaves" 2>&1) size: $(cat "$sys/size" 2>&1) name: $(cat "$sys/dm/name" 2>&1)"
done
echo "  read root first 1 KiB: $(dd if="$PMOS_ROOT" bs=1024 count=1 2>/dev/null | wc -c) bytes"
echo "  read boot first 1 KiB: $(dd if="$PMOS_BOOT" bs=1024 count=1 2>/dev/null | wc -c) bytes"

echo
echo "=== 3. a second call refuses to take over existing devices ==="
PMOS_BOOT=""
PMOS_ROOT=""
if dm_attach_subpartitions "$P"; then
	fail "second call should have been refused"
fi
echo "  refused as expected: PMOS_BOOT='$PMOS_BOOT' PMOS_ROOT='$PMOS_ROOT'"
dmsetup table s5tmmcblk0p21p1 >/dev/null 2>&1 || fail "the refusal removed a live mapping"
echo "  the live mapping is still there: $(dmsetup table s5tmmcblk0p21p1)"

echo
echo "=== 4. guards refuse partitions without the recorded layout ==="
for other in /dev/block/mmcblk0p18 /dev/block/mmcblk0p19 /dev/block/mmcblk0p20; do
	[ -b "$other" ] || continue
	PMOS_BOOT=""
	PMOS_ROOT=""
	if dm_attach_subpartitions "$other"; then
		fail "a guard accepted $other"
	fi
	echo "  $other refused"
done
echo "  still only the two s5t mappings: $(dmsetup info -c --noheadings -o name | grep -c '^s5tmmcblk')"

echo
echo "=== 5. cleanup ==="
dmsetup remove s5tmmcblk0p21p2 && dmsetup remove s5tmmcblk0p21p1
rm -f /dev/mapper/s5tmmcblk0p21p1 /dev/mapper/s5tmmcblk0p21p2
echo "--- state after (the kpartx maintenance mappings must be untouched) ---"
dmsetup ls --tree -c 2>&1
dmsetup table mmcblk0p21p1
dmsetup table mmcblk0p21p2
echo
echo "TEST PASSED"
