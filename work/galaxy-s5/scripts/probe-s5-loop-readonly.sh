#!/sbin/sh
# TWRP-only, read-only comparison of the installed initramfs losetup modes.
set -u
root=/tmp/codex-s5-native
source=/dev/mmcblk0p21
binds=""
loop=""
scratch="$(mktemp -d /tmp/s5-loop-probe.XXXXXXXX)" || exit 1
cleanup() {
	if [ -n "$loop" ]; then
		chroot "$root" /usr/sbin/losetup -d "$loop" >/dev/null 2>&1 || true
	fi
	for path in $binds; do
		umount "$root/$path" >/dev/null 2>&1 || true
	done
	rm -rf "$scratch"
}
trap cleanup EXIT HUP INT TERM

[ -d "$root" ] && [ -b "$source" ] || { echo 'Missing mounted native root or p21'; exit 1; }
[ "$(cat /sys/class/block/mmcblk0p21/partition)" = 21 ] || exit 1
for path in dev sys proc; do
	if ! grep -q " $root/$path " /proc/mounts; then
		mount -o bind "/$path" "$root/$path" || exit 1
		binds="$path $binds"
	fi
done
[ "$(chroot "$root" /usr/sbin/losetup --version)" = 'losetup from util-linux 2.42.4' ] || exit 1
[ -b "$root/dev/mmcblk0p21" ] || exit 1

dd if="$source" of="$scratch/baseline" bs=512 count=1 2>/dev/null || exit 1
[ "$(wc -c < "$scratch/baseline")" -eq 512 ] || exit 1
echo 'backing p21 sector0:'
sha256sum "$scratch/baseline" | cut -d ' ' -f 1

for mode in on off; do
	loop="$(chroot "$root" /usr/sbin/losetup -f)" || exit 1
	case "$loop" in /dev/loop[0-9]*) ;; *) echo 'Unexpected free loop path'; exit 1 ;; esac
	echo "direct-io=$mode loop=$loop"
	if chroot "$root" /usr/sbin/losetup --read-only --show -P --direct-io="$mode" "$loop" "$source"; then
		if dd if="$loop" of="$scratch/sector" bs=512 count=1 2>/dev/null && [ "$(wc -c < "$scratch/sector")" -eq 512 ]; then
			echo 'sector0 read: success; SHA256:'
			sha256sum "$scratch/sector" | cut -d ' ' -f 1
		else
			echo 'sector0 read: FAILED'
		fi
		name="${loop##*/}"
		for part in 1 2; do
			path="/sys/class/block/${name}p${part}"
			if [ -e "$path/partition" ]; then
				echo "p$part start=$(cat "$path/start") size=$(cat "$path/size")"
			else
				echo "p$part absent"
			fi
		done
	else
		echo 'losetup attach: FAILED'
	fi
	chroot "$root" /usr/sbin/losetup -d "$loop" >/dev/null 2>&1 || { echo 'Detach failed; stop'; exit 1; }
	loop=""
done
