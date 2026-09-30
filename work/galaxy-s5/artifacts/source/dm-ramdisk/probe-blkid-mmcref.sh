#!/bin/sh
# Fourth read-only probe: confirm that util-linux blkid's *search* modes do
# work in this environment for mmc partitions, so the empty results for the
# device-mapper nodes can be attributed to the dm major number. Also check how
# the device-mapper control node is reachable, because the initramfs has no
# mdev rule for it.

set -u

C=/tmp/codex-s5-maint/chroot
D=/tmp/codex-s5-probe
LOADER="$C/lib/ld-musl-armhf.so.1"
run() { "$LOADER" --library-path "$C/lib" "$D/busybox" "$@"; }
blkid() { "$LOADER" --library-path "$C/lib" "$D/blkid" "$@"; }

echo "=== blkid version ==="
blkid --version 2>&1
echo "=== control node candidates in TWRP ==="
run ls -la /dev/device-mapper /dev/control /dev/mapper/control 2>&1
run grep -E "device-mapper|control" /proc/misc
echo "=== mmc partition labels ==="
for p in 19 20 21 13 18; do
	echo "  p$p: $(run blkid /dev/block/mmcblk0p$p 2>&1)"
done
echo "=== blkid search modes against an mmc partition ==="
label=$(run blkid /dev/block/mmcblk0p19 2>/dev/null | run tr ' ' '\n' | run grep '^LABEL=' | run cut -d'"' -f2)
uuid=$(run blkid /dev/block/mmcblk0p19 2>/dev/null | run tr ' ' '\n' | run grep '^UUID=' | run cut -d'"' -f2)
echo "  CACHE label='$label' uuid='$uuid'"
if [ -n "$label" ]; then
	echo "  blkid -L $label  -> '$(blkid -L "$label" 2>&1)'"
fi
if [ -n "$uuid" ]; then
	echo "  blkid --uuid $uuid -> '$(blkid --uuid "$uuid" 2>&1)'"
fi
echo "  blkid /dev/block/mmcblk0p19 -> '$(blkid /dev/block/mmcblk0p19 2>&1)'"
echo "=== util-linux hardcoded dm major in the binary ==="
run strings "$D/blkid" | run grep -E "^(/dev/mapper/control|/sys/devices/virtual/block|/dev/control)$"
echo "PROBE DONE"
