#!/bin/sh
# Read-only probe of the postmarketOS initramfs userland under TWRP.
#
# It answers three questions before the device-mapper bypass is built:
#   1. which applets the initramfs busybox actually provides (awk? od? fdisk?)
#   2. the exact `fdisk -l` layout the initramfs sees for the nested USERDATA MBR
#   3. whether the initramfs `dmsetup` binary runs here and sees the linear target
#
# It only reads block devices and sysfs. No partition is written, formatted or
# mounted, and no device-mapper table is created.

set -u

C=/tmp/codex-s5-maint/chroot
D=/tmp/codex-s5-probe
P=/dev/block/mmcblk0p21
LOADER="$C/lib/ld-musl-armhf.so.1"

die() { echo "STOP: $*" >&2; exit 1; }

[ -x "$LOADER" ] || die "staged musl loader missing"
[ -b "$P" ] || die "physical USERDATA p21 absent"
[ -x "$C/bin/busybox" ] || die "staged busybox missing"
[ -r "$D/busybox" ] && [ -r "$D/busybox-extras" ] || die "initramfs binaries not pushed"

# The staged TWRP bootstrap busybox is expected to be the very same build the
# initramfs uses; verify instead of assuming.
staged_md5=$("$LOADER" --library-path "$C/lib" "$C/bin/busybox" md5sum "$C/bin/busybox" | awk '{print $1}')
pushed_md5=$("$LOADER" --library-path "$C/lib" "$D/busybox" md5sum "$D/busybox" | awk '{print $1}')
echo "staged busybox md5:  $staged_md5"
echo "initramfs busybox md5: $pushed_md5"
[ "$staged_md5" = "$pushed_md5" ] || echo "NOTE: bootstrap and initramfs busybox differ"

run() {
	"$LOADER" --library-path "$C/lib" "$D/busybox" "$@"
}
runx() {
	"$LOADER" --library-path "$C/lib" "$D/busybox-extras" "$@"
}

echo "=== 1. applets needed by the bypass ==="
run --list | sort > "$D/busybox.list" 2>/dev/null
runx --list | sort > "$D/busybox-extras.list" 2>/dev/null
for applet in awk fdisk od hexdump mknod blkid losetup mdev tr cut; do
	where=absent
	grep -qx "$applet" "$D/busybox.list" && where=busybox
	grep -qx "$applet" "$D/busybox-extras.list" && where=busybox-extras
	echo "  $applet: $where"
done
echo "  awk direct: $(run awk 'BEGIN{print "ok"}' 2>&1)"
echo "  awk extras: $(runx awk 'BEGIN{print "ok"}' 2>&1)"
echo "  fdisk direct: $(run fdisk 2>&1 | head -1)"

echo "=== 2. nested USERDATA MBR as the initramfs sees it ==="
echo "--- p21 size (512-byte sectors): $(cat /sys/class/block/mmcblk0p21/size)"
echo "--- logical block size: $(cat /sys/class/block/mmcblk0p21/queue/logical_block_size)"
echo "--- raw fdisk -l output ---"
run fdisk -l "$P" 2>&1
echo "--- raw fdisk -l (boot mapped partition p1) ---"
run fdisk -l /dev/mapper/mmcblk0p21p1 2>&1

echo "=== 3. device-mapper ==="
echo "--- /proc/misc ---"
cat /proc/misc 2>/dev/null
echo "--- /dev/mapper ---"
ls -la /dev/mapper 2>&1
echo "--- dmsetup version (initramfs binary) ---"
"$LOADER" --library-path "$C/lib:$D/lib" "$D/dmsetup" version 2>&1
echo "--- dmsetup targets ---"
"$LOADER" --library-path "$C/lib:$D/lib" "$D/dmsetup" targets 2>&1
echo "--- dmsetup ls ---"
"$LOADER" --library-path "$C/lib:$D/lib" "$D/dmsetup" ls --tree -c 2>&1
echo "--- existing nested tables ---"
"$LOADER" --library-path "$C/lib:$D/lib" "$D/dmsetup" table mmcblk0p21p1 2>&1
"$LOADER" --library-path "$C/lib:$D/lib" "$D/dmsetup" table mmcblk0p21p2 2>&1

echo "=== 4. mdev dm node rules (from the initramfs mdev.conf) ==="
echo "(the mdev.conf itself is on the Mac; it has no dm rules - see the source tree)"

echo "PROBE DONE"
