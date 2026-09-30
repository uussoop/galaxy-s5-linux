# Mount the existing native installation from fresh TWRP

This is a maintenance procedure for the **already installed** postmarketOS root and boot filesystems inside physical USERDATA `/dev/block/mmcblk0p21`. It does not run the installer, partition, format, flash BOOT, or touch external SD. The recorded nested MBR has p1 at sector 2048 for 497664 sectors (ext2, `pmOS_boot`) and p2 at sector 499712 for 24346624 sectors (ext4, `pmOS_root`). If any identity, size, layout, or label check fails, stop; do not substitute another block device by guesswork.

On the Mac, push only the already approved frozen a677 recovery ZIP into TWRP **RAM** (not `/sdcard` or `/external_sd`). Use the phone's explicit ADB serial:

```sh
ZIP=galaxy-s5-linux/work/galaxy-s5/artifacts/pmos-samsung-k3gxx-a6773e32-twrp-bind.zip
EXPECTED=a6773e32ea0a90078b6e8e53511376c20dbca7a46279e9eb9569a4c4f035b70b
[ "$(shasum -a 256 "$ZIP" | awk '{print $1}')" = "$EXPECTED" ] || exit 1
adb -s 0000000000000000 push \
  "$ZIP" \
  /tmp/codex-s5-maint.zip
[ "$(adb -s 0000000000000000 exec-out cat /tmp/codex-s5-maint.zip | shasum -a 256 | awk '{print $1}')" = "$EXPECTED" ] || exit 1
adb -s 0000000000000000 shell
```

In that one TWRP root shell, paste the following block. It extracts only BusyBox, kpartx, and their library directory from the ZIP into `/tmp`; it never executes `pmos_install`. The chroot bind mounts use `mount -o bind`, which was verified on this TWRP. The mapper is created in kernel memory. The native filesystems are first mounted **read-only**, with ext4 journal loading suppressed.

```sh
set -eu
die() { echo "STOP: $*" >&2; exit 1; }
ZIP=/tmp/codex-s5-maint.zip
C=/tmp/codex-s5-maint/chroot
R=/tmp/codex-s5-native
P=/dev/block/mmcblk0p21
M1=/dev/mapper/mmcblk0p21p1
M2=/dev/mapper/mmcblk0p21p2

[ -f "$ZIP" ] || die "approved ZIP absent from TWRP RAM"
[ -b "$P" ] || die "physical USERDATA p21 absent"
[ "$(cat /sys/class/block/mmcblk0p21/partition)" = 21 ] || die "wrong physical partition"
case "$(readlink -f /sys/class/block/mmcblk0p21)" in
  */mmcblk0/mmcblk0p21) ;;
  *) die "p21 is not a child of internal mmcblk0" ;;
esac
[ "$(cat /sys/class/block/mmcblk0p21/size)" -ge 24846336 ] || die "USERDATA too small"

mkdir -p /tmp/codex-s5-maint
unzip -o "$ZIP" 'chroot/bin/busybox' 'chroot/bin/kpartx' 'chroot/lib/*' \
  -d /tmp/codex-s5-maint >/dev/null || die "tool extraction failed"
[ -f "$C/bin/busybox" ] && [ -f "$C/bin/kpartx" ] || die "bootstrap tools missing"
chmod 755 "$C/bin/busybox" "$C/bin/kpartx" "$C"/lib/*
for d in dev proc sys; do
  mkdir -p "$C/$d"
  mount -o bind "/$d" "$C/$d" || die "bootstrap bind mount failed: $d"
done
bb() {
  "$C/lib/ld-musl-armhf.so.1" --library-path "$C/lib" "$C/bin/busybox" "$@"
}
kp() { chroot "$C" /bin/kpartx "$@"; }

if bb awk '$2=="/data" || $2=="/sdcard" {bad=1} END {exit bad ? 0 : 1}' /proc/mounts; then
  die "TWRP Data is mounted; unmount it before mapping native USERDATA"
fi
[ ! -e "$M1" ] && [ ! -e "$M2" ] || die "mapper already exists; inspect before proceeding"
layout=$(kp -l "$P") || die "cannot read nested partition table"
printf '%s\n' "$layout" | bb awk -v source="$P" '
  $1=="mmcblk0p21p1" && $2==":" && $3==0 && $4==497664 && $5==source && $6==2048 {boot++; next}
  $1=="mmcblk0p21p2" && $2==":" && $3==0 && $4==24346624 && $5==source && $6==499712 {root++; next}
  {bad=1}
  END {exit !(boot==1 && root==1 && !bad && NR==2)}
' || die "nested MBR layout differs from recorded installation"
kp -as "$P" || die "kpartx mapping failed"

check_map() {
  n=$1 expected_size=$2 map=/dev/mapper/mmcblk0p21p$1
  [ -b "$map" ] || die "mapper p$n absent"
  # TWRP kpartx creates real block nodes here, not necessarily symlinks.
  hex_dev=$(bb stat -c '%t:%T' "$map") || die "cannot stat mapper p$n"
  major_hex=${hex_dev%:*} minor_hex=${hex_dev#*:}
  case "$major_hex:$minor_hex" in *[!0-9A-Fa-f:]*|:*|*:) die "invalid mapper device number" ;; esac
  decimal_dev="$((0x$major_hex)):$((0x$minor_hex))"
  dm_sys=$(bb readlink -f "/sys/dev/block/$decimal_dev") || die "mapper p$n lacks sysfs identity"
  base=${dm_sys##*/}
  case "$base" in dm-[0-9]*) ;; *) die "mapper p$n is not a DM device" ;; esac
  [ "$(cat /sys/class/block/$base/dev)" = "$decimal_dev" ] || die "mapper device number mismatch"
  [ "$(cat /sys/class/block/$base/dm/name)" = "mmcblk0p21p$n" ] || die "mapper name mismatch"
  [ -e "/sys/class/block/$base/slaves/mmcblk0p21" ] || die "mapper parent is not USERDATA p21"
  [ "$(cat /sys/class/block/$base/size)" = "$expected_size" ] || die "mapper size mismatch"
}
check_map 1 497664
check_map 2 24346624
boot_id=$(bb blkid "$M1") || die "cannot identify boot filesystem"
root_id=$(bb blkid "$M2") || die "cannot identify root filesystem"
case "$boot_id" in *'LABEL="pmOS_boot"'*) ;; *) die "boot label mismatch" ;; esac
case "$boot_id" in *'TYPE="ext2"'*) ;; *) die "boot is not ext2" ;; esac
case "$root_id" in *'LABEL="pmOS_root"'*) ;; *) die "root label mismatch" ;; esac
case "$root_id" in *'TYPE="ext4"'*) ;; *) die "root is not ext4" ;; esac

mkdir -p "$R"
mount -t ext4 -o ro,noload "$M2" "$R" || die "root read-only mount failed"
[ -d "$R/boot" ] && [ ! -L "$R/boot" ] || die "native /boot is not a directory"
bb grep -Eq '^ID=(postmarketos|"postmarketos")$' "$R/etc/os-release" || die "not a postmarketOS root"
mount -t ext2 -o ro "$M1" "$R/boot" || die "boot read-only mount failed"
mount_check() {
  bb awk -v src="$1" -v target="$2" -v type="$3" \
    '$1==src && $2==target && $3==type {ok=1} END {exit !ok}' /proc/mounts
}
mount_check "$M2" "$R" ext4 || die "root mount source mismatch"
mount_check "$M1" "$R/boot" ext2 || die "boot mount source mismatch"
echo "Native root and boot verified read-only at $R"
```

**Only after** the intended replacement boot image, DT, and matching kernel APK have been separately reviewed and checksummed, switch these exact filesystems to read-write in the same TWRP shell. This block does not copy or install anything:

```sh
umount "$R/boot" || die "cannot unmount read-only boot"
umount "$R" || die "cannot unmount read-only root"
mount -t ext4 -o rw "$M2" "$R" || die "root read-write mount failed"
mount -t ext2 -o rw "$M1" "$R/boot" || die "boot read-write mount failed"
mount_check "$M2" "$R" ext4 || die "root source changed"
mount_check "$M1" "$R/boot" ext2 || die "boot source changed"
bb grep -Eq '^ID=(postmarketos|"postmarketos")$' "$R/etc/os-release" || die "root identity changed"
for d in dev proc sys; do
  mkdir -p "$R/$d"
  mount -o bind "/$d" "$R/$d" || die "native chroot bind failed: $d"
done
echo "Native root and boot verified read-write; stage reviewed artifacts now"
```

For an eventual repair, stage the **matching** kernel APK and corrected BOOT/DT files in TWRP `/tmp`, verify each expected SHA-256, then install the APK inside the mounted native root and copy the corrected boot artifacts into its `/boot`. Review the APK install scripts first, because they may regenerate or write boot files. Copy the final corrected files last, verify their hashes at the destination, and `sync`. Do not use the recovery installer, `parted`, `mkfs`, or any path under `/external_sd`. The existing Wi-Fi profile, SSH host key, and `/home/user` stay in p2 and should not be replaced.

Cleanup works in the same shell **or a new TWRP root shell after a failed guard**. It redeclares its paths and only unmounts paths that are actually mounted. If an unmount fails, stop and inspect; do not force-detach or delete a mounted directory. The staged tools and ZIP can remain in TWRP tmpfs until recovery reboots.

```sh
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
```
