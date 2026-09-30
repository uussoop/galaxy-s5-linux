#!/sbin/sh
set -eu
die() { echo "STOP: $*" >&2; exit 1; }
C=/tmp/codex-s5-maint/chroot
R=/tmp/codex-s5-native
bb() { "$C/lib/ld-musl-armhf.so.1" --library-path "$C/lib" "$C/bin/busybox" "$@"; }
mounted() {
    bb awk -v src="$1" -v target="$2" -v type="$3" \
        '$1==src && $2==target && $3==type {ok=1} END {exit !ok}' /proc/mounts
}
mounted /dev/mapper/mmcblk0p21p2 "$R" ext4 || die "native root mount differs"
mounted /dev/mapper/mmcblk0p21p1 "$R/boot" ext2 || die "native boot mount differs"
[ "$(cat /sys/class/block/dm-1/dm/name)" = mmcblk0p21p2 ] || die "root mapper name differs"
[ "$(cat /sys/class/block/dm-1/size)" = 24346624 ] || die "root mapper size differs"
[ -e /sys/class/block/dm-1/slaves/mmcblk0p21 ] || die "root mapper parent differs"
[ "$(bb stat -c '%t:%T' /dev/mapper/mmcblk0p21p2)" = fe:1 ] || die "root mapper device differs"
[ "$(bb sha256sum /tmp/s5-openrc-trace | bb awk '{print $1}')" = 7b832173262bfb5387a832b8d346ba2c396cf960f6912a59629b0b87d5680bef ] || die "wrapper hash differs"
bb grep -Eq '^ID=(postmarketos|"postmarketos")$' "$R/etc/os-release" || die "native OS differs"
[ -f "$R/etc/s5-boot-diagnostics/rc.conf.original" ] || die "original rc.conf absent"
[ ! -e "$R/etc/s5-boot-diagnostics/inittab.original" ] || die "inittab already backed up; inspect before retry"
[ ! -e "$R/usr/sbin/s5-openrc-trace" ] || die "wrapper already exists; inspect before retry"
[ "$(bb grep -Ec '^::sysinit:/sbin/openrc (sysinit|boot)$|^::wait:/sbin/openrc default$' "$R/etc/inittab")" = 3 ] || die "startup entries differ"
umount "$R/boot" || die "boot unmount failed"
umount "$R" || die "root unmount failed"
mount -t ext4 -o rw /dev/mapper/mmcblk0p21p2 "$R" || die "root writable mount failed"
mounted /dev/mapper/mmcblk0p21p2 "$R" ext4 || die "writable root source differs"
bb grep -Eq '^ID=(postmarketos|"postmarketos")$' "$R/etc/os-release" || die "root identity changed"
bb cp -p "$R/etc/inittab" "$R/etc/s5-boot-diagnostics/inittab.original"
bb chown 0:0 "$R/etc/s5-boot-diagnostics"
bb cp /tmp/s5-openrc-trace "$R/usr/sbin/s5-openrc-trace"
bb chown 0:0 "$R/usr/sbin/s5-openrc-trace"
bb chmod 755 "$R/usr/sbin/s5-openrc-trace"
bb mkdir -p "$R/var/log/s5-boot-diagnostics"
bb chown 0:0 "$R/var/log/s5-boot-diagnostics"
bb chmod 700 "$R/var/log/s5-boot-diagnostics"
bb cp -p "$R/etc/inittab" "$R/etc/inittab.s5diag.tmp"
bb sed \
    -e 's|^::sysinit:/sbin/openrc sysinit$|::sysinit:/usr/sbin/s5-openrc-trace sysinit|' \
    -e 's|^::sysinit:/sbin/openrc boot$|::sysinit:/usr/sbin/s5-openrc-trace boot|' \
    -e 's|^::wait:/sbin/openrc default$|::wait:/usr/sbin/s5-openrc-trace default|' \
    "$R/etc/inittab" > "$R/etc/inittab.s5diag.tmp"
[ "$(bb grep -Ec '^::sysinit:/usr/sbin/s5-openrc-trace (sysinit|boot)$|^::wait:/usr/sbin/s5-openrc-trace default$' "$R/etc/inittab.s5diag.tmp")" = 3 ] || die "replacement entries invalid"
chroot "$R" /bin/busybox sh -n /usr/sbin/s5-openrc-trace || die "native wrapper syntax failed"
[ "$(bb sha256sum "$R/usr/sbin/s5-openrc-trace" | bb awk '{print $1}')" = 7b832173262bfb5387a832b8d346ba2c396cf960f6912a59629b0b87d5680bef ] || die "installed wrapper hash differs"
[ "$(bb stat -c '%a:%u:%g' "$R/usr/sbin/s5-openrc-trace")" = 755:0:0 ] || die "wrapper permissions differ"
bb mv "$R/etc/inittab.s5diag.tmp" "$R/etc/inittab"
bb diff -u "$R/etc/s5-boot-diagnostics/inittab.original" "$R/etc/inittab" || [ "$?" = 1 ]
sync
echo S5_OPENRC_TRACE_INSTALLED_VERIFIED
