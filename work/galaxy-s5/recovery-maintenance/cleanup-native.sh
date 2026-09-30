set -eu
die() { echo "STOP: $*" >&2; exit 1; }
C=/tmp/codex-s5-maint/chroot
R=/tmp/codex-s5-native
P=/dev/block/mmcblk0p21
[ -x "$C/lib/ld-musl-armhf.so.1" ] && [ -x "$C/bin/busybox" ] || die "staged BusyBox is unavailable"
bb() {
  "$C/lib/ld-musl-armhf.so.1" --library-path "$C/lib" "$C/bin/busybox" "$@"
}
mounted_at() {
  bb awk -v target="$1" '$2==target {found=1} END {exit !found}' /proc/mounts
}
bb sync
for d in sys proc dev; do
  if mounted_at "$R/$d"; then
    umount "$R/$d" || die "native chroot bind still mounted: $d"
  fi
done
if mounted_at "$R/boot"; then umount "$R/boot" || die "boot still mounted"; fi
if mounted_at "$R"; then umount "$R" || die "root still mounted"; fi
if [ -e /dev/mapper/mmcblk0p21p1 ] || [ -e /dev/mapper/mmcblk0p21p2 ]; then
  [ -x "$C/bin/kpartx" ] && mounted_at "$C/dev" || die "mapper exists without working bootstrap"
  chroot "$C" /bin/kpartx -ds "$P" || die "could not remove nested mapper devices"
fi
for d in sys proc dev; do
  if mounted_at "$C/$d"; then umount "$C/$d" || die "bootstrap bind still mounted: $d"; fi
done
echo "Native mounts, mapper devices, and bootstrap binds released"
