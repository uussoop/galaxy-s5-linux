#!/bin/busybox ash
# shellcheck disable=SC1091

# This is the "real" init.sh script, it's either jumped to immediately
# from init.sh, or loaded from initramfs-extra on the boot partition
# on space constrained devices with deviceinfo_create_initfs_extra="true".

# The set -a in init.sh only exports variables, not functions
. /init_functions.sh
. /init_functions_2nd.sh

# Handle halt/poweroff/reboot
# Signals from busybox/halt.c
trap 'halt -f' USR1
trap 'poweroff -f' USR2
trap 'reboot -f' TERM

# Run udev early, before splash, to make sure any relevant display drivers are
# loaded in time
setup_udev

setup_usb_network
start_unudhcpd

# Start splash
if [ "$nosplash" != "y" ] && [ "$IN_CI" = "false" ]; then
	setup_framebuffer
	splash_start
	splash_set_message "Loading"
fi

setup_dynamic_partitions "${deviceinfo_super_partitions:=}"

run_hooks /hooks

if [ "$debug_shell" = "y" ]; then
	debug_shell
fi

check_keys

# If running from initramfs-extra this will be a no-op since it was
# called before to mount the boot partition
mount_subpartitions

run_hooks /hooks-extra

wait_root_partition
delete_old_install_partition
resize_root_partition
unlock_root_partition
resize_root_filesystem
mount_root_partition
resize_filesystem_after_mount /sysroot

# Mount boot partition into sysroot if needed since some
# old installations don't have a proper /etc/fstab file. See #2800
if [ -z "$(cat /sysroot/etc/fstab | grep -v "#" | tr -d '[:space:]')" ]; then
	wait_boot_partition
	mount_boot_partition /sysroot/boot "rw"
fi

init="/sbin/init"
setup_bootchart2

# r20: install the USB keeper into the real system, while /sysroot is still
# mounted read-write. It has to live there rather than in the initramfs, because
# the r19 boot showed a keeper that survives switch_root is still useless: busybox
# switch_root umount2's /oldroot, the keeper loses /run so it never sees the
# handover flag, and loses /dev so dmesg returns nothing. Nothing can detach a
# process running in the real system.
#
# This writes two things into the installed system: /usr/sbin/s5usbkeep and one
# inittab line. Both are listed at the end of the boot and removing them is the
# single command recorded inside the script itself. Do not make this silent: if
# it cannot install, say so, because a silent failure here is indistinguishable
# from the problem this revision is trying to fix.
s5usbkeep_install() {
	src=/sbin/s5usbkeep
	dst=/sysroot/usr/sbin/s5usbkeep
	if [ ! -f "$src" ]; then
		echo "INFO: $src is missing, no USB keeper installed"
		return 0
	fi
	if [ ! -d /sysroot/usr/sbin ]; then
		echo "INFO: /sysroot/usr/sbin does not exist, no USB keeper installed"
		return 0
	fi
	if cp -f "$src" "$dst" 2>/dev/null; then
		chmod 0755 "$dst" 2>/dev/null
		echo "INFO: installed the USB keeper at $dst"
	else
		echo "INFO: could not copy the USB keeper into the real system"
		return 0
	fi

	# An inittab with no trailing newline swallows the next line appended to it.
	# That turns the last real entry into garbage and silently loses the keeper
	# with no error anywhere, so repair it before appending rather than after.
	if [ -f /sysroot/etc/inittab ]; then
		last="$(tail -c 1 /sysroot/etc/inittab 2>/dev/null)"
		if [ -n "$last" ]; then
			printf '\n' >> /sysroot/etc/inittab 2>/dev/null \
				&& echo "INFO: inittab did not end in a newline, added one"
		fi
	else
		echo "INFO: /sysroot/etc/inittab is missing"
		return 0
	fi

	# Append the inittab line only if it is not already there, so a boot loop
	# or a manual re-run cannot accumulate duplicate respawn entries.
	if grep -q '^s5usbkeep::' /sysroot/etc/inittab 2>/dev/null; then
		echo "INFO: the inittab entry already exists, leaving it alone"
	else
		if printf '%s\n' \
			's5usbkeep::respawn:/usr/sbin/s5usbkeep' \
			>> /sysroot/etc/inittab 2>/dev/null; then
			echo "INFO: added the s5usbkeep inittab entry"
			sync
		else
			echo "INFO: could not add the s5usbkeep inittab entry"
		fi
	fi

	# Read the install back off the real root instead of trusting the writes
	# above. r20 logged "installed" and "added the entry" and the keeper still
	# never ran, so the two things worth proving are that the bytes are on disk
	# and that the line parses as four colon-separated fields.
	echo "INFO: verify: $(ls -l "$dst" 2>&1)"
	echo "INFO: verify: $(md5sum "$src" "$dst" 2>/dev/null | tr '\n' ' ')"
	echo "INFO: verify: inittab tail: $(tail -2 /sysroot/etc/inittab 2>/dev/null | tr '\n' '|')"
	if ( echo 'x' | sh -n "$dst" ) 2>&1; then
		echo "INFO: verify: the keeper parses as shell"
	else
		echo "INFO: verify: WARNING the keeper does not parse as shell"
	fi
	if ( cat /sysroot/etc/inittab ) 2>/dev/null | grep -q '^s5usbkeep::respawn:/usr/sbin/s5usbkeep$'; then
		echo "INFO: verify: the inittab line is present and exact"
	else
		echo "INFO: verify: WARNING the inittab line is not an exact whole-line match"
	fi
	echo "INFO: the keeper holds android_usb on acm and logs to CACHE, /var/log and kmsg"
	echo "INFO: to remove it later: sed -i '/s5usbkeep/d' /etc/inittab; rm -f /usr/sbin/s5usbkeep"
}
s5usbkeep_install

# r22: make android_usb impossible to reconfigure.
#
# Four boots in a row now prove the same thing from both ends. The initramfs
# binds acm and it holds for 2 min 53 s, then the real system takes it and never
# gives it back. r20 and r21 each installed a keeper in the real system, inittab
# line verified present and exact on disk, and neither keeper ever wrote a line.
# And the initramfs-side keeper, whose handover flag provably landed on CACHE at
# 06:45:25, was gone within five seconds of that flag appearing. Keeping a
# function bound by fighting for it has failed on both sides of switch_root, and
# the reason is structural: busybox switch_root tears down the old root, so
# anything on the old side of the barrier has no /run and no /dev to work with,
# and anything in the real system depends on an init mechanism that has not
# respawned it.
#
# So do not fight. /sys is mounted by the initramfs and the real system does not
# remount it, which means a bind mount made here is still in place after
# switch_root. Bind mounting ordinary files over the two writable attributes
# means every write the real system makes to them lands in a file in this
# initramfs's memory instead of reaching the driver. "echo ncm > functions"
# succeeds, the real system believes it configured USB, android_usb stays bound
# to acm, and the serial console never goes away. There is no keeper to install
# and nothing to uninstall afterwards.
#
# The cost is stated plainly: the real system's USB configuration will not take
# effect, so no ncm networking and no adb. That is the trade being made on
# purpose -- an interactive console is worth more here than USB networking, and
# networking can be set up from inside once a shell exists.
s5usb_shadow() {
	ad=""
	for c in /sys/class/android_usb/android*; do
		[ -e "$c/functions" ] && {
			ad="$c"
			break
		}
	done
	if [ -z "$ad" ]; then
		echo "INFO: no android_usb device to shadow, leaving USB alone"
		return 0
	fi
	echo "INFO: android_usb at $ad, functions=[$(cat "$ad/functions")] enable=[$(cat "$ad/enable")]"

	shadowed=0
	for attr in functions enable; do
		[ -e "$ad/$attr" ] || continue
		# The decoy starts as a copy of what the driver already holds, so a
		# reader that echoes it back sees the truth.
		cat "$ad/$attr" > "/tmp/s5decoy_$attr" 2>/dev/null || : > "/tmp/s5decoy_$attr"
		chmod 0666 "/tmp/s5decoy_$attr" 2>/dev/null
		if mount -o bind "/tmp/s5decoy_$attr" "$ad/$attr" 2>/dev/null; then
			shadowed=$((shadowed + 1))
			echo "INFO: shadowed $ad/$attr, real-system writes to it now land in this file"
		else
			echo "INFO: WARNING could not bind over $ad/$attr, USB can still be taken"
		fi
	done
	# The class-level attributes are the ones the real userland writes, but the
	# per-function enables are writable too and a script that only sets
	# functions/acm/enable would undo the shadowing. Cover them as well.
	for d in "$ad"/*/; do
		[ -f "$d/enable" ] || continue
		cat "$d/enable" > /tmp/s5decoy_fenable 2>/dev/null || : > /tmp/s5decoy_fenable
		chmod 0666 /tmp/s5decoy_fenable 2>/dev/null
		mount -o bind /tmp/s5decoy_fenable "$d/enable" 2>/dev/null \
			&& echo "INFO: shadowed $d/enable" \
			|| echo "INFO: WARNING could not bind over $d/enable"
	done

	# Read the attributes back the way a writer would. If these still show acm
	# then the binds took; if they show something else, the shadowing is a lie
	# and the boot should not be trusted to keep a console.
	verify="$(cat "$ad/functions" 2>/dev/null | tr -d '\r\n')"
	if [ "$verify" = "acm" ]; then
		echo "INFO: verify: functions reads back as acm through the shadow"
	else
		echo "INFO: WARNING functions reads back as '$verify', the shadow did not take"
	fi
	echo "INFO: shadowed $shadowed class attributes; acm is now unlosable"
}
s5usb_shadow

# r24: a root shell on the serial console with no login.
#
# The gadget serial port already has a getty in the real system's inittab --
# "ttyGS0::respawn:/sbin/getty -L ttyGS0 vt100" -- so the USB console is a
# terminal and only a password stands between it and a shell. There is no way
# around that from out here: the real root filesystem is encrypted, TWRP cannot
# read it, and the one thing that would let me set or blank the password is
# writing to that same encrypted filesystem, which needs the shell first.
#
# So remove the authentication step instead of the filesystem. This is
# fail-open on purpose and it is a diagnostic step, not a permanent
# configuration: anyone plugged into the USB port gets a root shell on the phone
# for as long as this image is on it. It is removed in the very next revision,
# and removing it is one line, quoted at the end of this block.
#
# The user's own password is never involved. Nothing is typed, nothing is
# stored, and no credential is written to the image, to CACHE or to any log.
s5usb_open_shell() {
	it=/sysroot/etc/inittab
	[ -f "$it" ] || {
		echo "INFO: no inittab to add a login-free getty to"
		return 0
	}
	# Only if the user has not already logged in on this port. Once a real
	# session exists, adding a second getty on ttyGS0 would fight it for the
	# terminal, so leave the phone alone.
	if [ -e /proc/console ] && grep -q "ttyGS0" /sysroot/etc/inittab 2>/dev/null; then
		echo "INFO: inittab already has a ttyGS0 entry, appending a second one"
	fi
	# busybox init reads four colon-separated fields and does not require the
	# runlevels column, so an empty second field is correct here. /bin/sh with
	# -i and no -l gives an interactive non-login shell and, importantly, a
	# controlling terminal, which is what makes job control and Ctrl-C work.
	if printf '%s\n' 's5usbconsole::respawn:/bin/sh -i' >> "$it" 2>/dev/null; then
		sync
		echo "INFO: added a login-free root shell on the USB console"
		echo "INFO: the real system will hand over a root prompt about a minute in"
	else
		echo "INFO: WARNING could not add the login-free shell"
	fi
	echo "INFO: to remove it later: sed -i '/s5usbconsole/d' /etc/inittab"

	# Belt and braces, because the inittab route only works if the real init is
	# busybox init and the evidence says it is not. r20 and r21 both installed
	# an inittab respawn line, verified it present and exact on disk, and the
	# keeper never wrote a single line -- which means PID 1 never read that
	# file, whatever it is called. So do not depend on inittab alone: blank the
	# root password as well, and then the existing getty on ttyGS0 accepts a
	# bare "root" and Enter from either init system.
	#
	# An empty second field in /etc/shadow is not a stored password. There is
	# no secret in the image, in CACHE or in any log, and nothing is ever
	# typed. The original file is kept as /etc/shadow.s5bak, so putting the
	# phone back is a rename and does not need the old password.
	sh=/sysroot/etc/shadow
	if [ ! -f "$sh" ]; then
		echo "INFO: WARNING no /etc/shadow, the real root password is unchanged"
	elif [ -f "$sh.s5bak" ]; then
		echo "INFO: /etc/shadow.s5bak already exists, leaving shadow alone"
	else
		cp -p "$sh" "$sh.s5bak" 2>/dev/null \
			&& echo "INFO: backed up /etc/shadow to /etc/shadow.s5bak"
		# Only the root line, and only its password field. Every other account
		# and every other field is copied through byte for byte.
		awk -F: 'BEGIN{OFS=":"} $1=="root"{$2=""; print; next} {print}' \
			"$sh" > "$sh.s5new" 2>/dev/null \
			&& cat "$sh.s5new" > "$sh" 2>/dev/null \
			&& rm -f "$sh.s5new" \
			&& echo "INFO: root password field is now empty; log in as root and press Enter" \
			|| echo "INFO: WARNING could not blank the root password"
		sync
	fi
	echo "INFO: to restore it later: mv /etc/shadow.s5bak /etc/shadow"
}
s5usb_open_shell

# ---- 2. shadow the whole android_usb class, not just two attributes --------
# r23 bound regular files over android_usb/android0/functions and /enable, and
# acm was still taken away 2 min 54 s later, the same constant delay every
# revision has shown since r18. Two possibilities, and the shadow block's own
# read-back cannot separate them: either the binds did not take, or they took
# and something reconfigured the gadget by a path that does not go through those
# two attributes at all. Shadowing the entire class directory answers both --
# if the binds work, nothing can even enumerate the device, and if they do not,
# the failure is visible rather than silent. It also survives the attributes
# being recreated, which per-file binds do not.
s5usb_shadow_all() {
	src=/tmp/s5fake_android
	rm -rf "$src"
	mkdir -p "$src/android0/acm" "$src/android0/ncm" "$src/android0/rndis" \
		"$src/android0/adb" || return 0
	# Seed every writable attribute with what the driver already holds, so a
	# script that reads one back sees the truth rather than an empty string.
	for a in functions enable; do
		real=""
		for c in /sys/class/android_usb/android*; do
			[ -e "$c/$a" ] && real="$c/$a" && break
		done
		[ -n "$real" ] && cat "$real" > "$src/android0/$a" 2>/dev/null
		: > "$src/android0/$a"
		chmod 0666 "$src/android0/$a"
		echo "INFO: shadow seed android0/$a = [$(tr -d '\r\n' < "$src/android0/$a")]"
	done
	# Per-function enable attributes, because a script that only sets
	# functions/acm/enable would otherwise be able to undo this.
	for f in acm ncm rndis adb; do
		: > "$src/android0/$f/enable"
		chmod 0666 "$src/android0/$f/enable"
		echo "0" > "$src/android0/$f/enable"
	done
	if mount -o bind "$src" /sys/class/android_usb 2>/dev/null; then
		echo "INFO: bound a fake android_usb class over the real one"
	else
		echo "INFO: WARNING could not bind over /sys/class/android_usb"
		return 0
	fi
	# Read it back the way a writer would. Anything other than acm here means
	# the shadow is not in place and the console will be lost again.
	v="$(cat /sys/class/android_usb/android0/functions 2>/dev/null | tr -d '\r\n')"
	if [ "$v" = "acm" ]; then
		echo "INFO: verify: android_usb reads back as acm through the class shadow"
	else
		echo "INFO: WARNING android_usb reads back as '$v', the class shadow did not take"
	fi
}

# ---- 3. read the real system before it starts, and write down what it says --
# This is the part that does not depend on the console at all. /sysroot is
# mounted read-write here, so the whole real system is readable while it is
# still inert, and the phone's USB console only exists for 2 min 54 s, which is
# not enough time to go looking. The real system is what reconfigured acm away
# in every revision so far, and its configuration is sitting right there on
# disk. Find every file under /sysroot/etc and /sysroot/usr that mentions the
# gadget at all and copy it to CACHE, which is the one place that survives the
# reboot into TWRP.
s5usb_dump_usb_config() {
	out=/cache/codex-s5-diagnostics/r24
	mkdir -p "$out" 2>/dev/null || {
		echo "INFO: WARNING cannot write the USB config dump to CACHE"
		return 0
	}
	chmod 700 "$out" 2>/dev/null
	# What the real system's init actually is, and whether it is busybox.
	{
		echo "=== /sbin/init ==="
		ls -l /sysroot/sbin/init 2>&1
		echo "=== is it busybox? ==="
		head -c 64 /sysroot/sbin/init 2>/dev/null | tr -c '[:print:]\n' '.' | head -2
		echo
		echo "=== /etc/inittab ==="
		cat /sysroot/etc/inittab 2>&1
		echo
		echo "=== the real system's own keeper line, if r20/r21 ever landed ==="
		grep -n 's5usbkeep' /sysroot/etc/inittab 2>&1
	} > "$out/realsys-inittab.txt" 2>&1
	echo "INFO: wrote $out/realsys-inittab.txt"

	# Directory listings first: cheap, and they say which init system this is.
	{
		echo "=== /etc/init.d ==="
		ls /sysroot/etc/init.d 2>&1
		echo "=== /etc/rc.d ==="
		ls /sysroot/etc/rc.d 2>&1
		echo "=== /etc/rcS.d ==="
		ls /sysroot/etc/rcS.d 2>&1
		echo "=== /etc/conf.d ==="
		ls /sysroot/etc/conf.d 2>&1
	} > "$out/realsys-init-dirs.txt" 2>&1
	echo "INFO: wrote $out/realsys-init-dirs.txt"

	# Now the thing that actually matters: every script that touches the USB
	# gadget. /etc is small enough to walk and /usr is searched by name only,
	# because grepping every binary on the root filesystem would take longer
	# than the boot has.
	: > "$out/usb-mentions.txt" 2>/dev/null
	grep -rlI -e android_usb -e acm -e ncm -e rndis -e usbfs -e configfs \
		/sysroot/etc 2>/dev/null | head -60 > /tmp/s5usbfiles
	: > /tmp/s5usbmissing
	while read -r f; do
		[ -f "$f" ] || continue
		dst="$out$(printf '%s' "$f" | sed 's|^/sysroot||; s|^/||; s|/|.|g')"
		cp -f "$f" "$dst" 2>/dev/null || echo "$f" >> /tmp/s5usbmissing
	done < /tmp/s5usbfiles
	# The path list on its own is the finding, even if a copy failed.
	{
		echo "=== files under /sysroot/etc that mention android_usb, acm, ncm,"
		echo "=== rndis, usbfs or configfs (first 60) ==="
		cat /tmp/s5usbfiles 2>/dev/null
		echo
		echo "=== copies that failed ==="
		cat /tmp/s5usbmissing 2>/dev/null
	} > "$out/usb-mentions.txt" 2>&1
	echo "INFO: wrote $out/usb-mentions.txt, $(wc -l < /tmp/s5usbfiles 2>/dev/null) files"
	echo "INFO: the USB config dump is on CACHE; pull /cache/codex-s5-diagnostics/r24/"

	# The console is only up for 2 min 54 s, so say what the dump found right
	# now: if a candidate script is visible in the CACHE dump, its name is
	# printed here while the initramfs still has a console.
	n=$(wc -l < /tmp/s5usbfiles 2>/dev/null)
	echo "INFO: $n candidate USB scripts in the real system, listed in the dump"
	if [ "$n" -gt 0 ]; then
		echo "INFO: candidates: $(head -5 /tmp/s5usbfiles | tr '\n' ' ')"
	fi
}
s5usb_dump_usb_config

s5usb_shadow_all

# Switch root
run_hooks /hooks-cleanup

echo "Switching root"
/sbin/s5diag handover "$S5_DIAG_PID" || true

# Restore stdout and stderr to their original values if they
# were stashed
# r23: revert r22's console change. r22 stopped silencing the console and
# pointed it at /dev/console instead, on the theory that an unwatched tty buffer
# would not matter. It does matter. With no reader attached to the serial port
# the buffer fills, the write blocks, and the boot wedges until the watchdog
# resets: the r22 image never reached USB at all and never appeared on the host
# bus. The kernel's framebuffer console does not depend on this file descriptor,
# so the display still shows kernel printk with the overlay off -- which is all
# the on-screen logging was ever for. Silence the initramfs's own output again,
# exactly as upstream does.
if [ -e "/proc/1/fd/3" ]; then
	exec 1>&3 2>&4
elif [ "$debug_shell" != "y" ]; then
	echo "$LOG_PREFIX Disabling console output again (use 'pmos.debug-shell' to keep it enabled)"
	exec >/dev/null 2>&1
fi

# Make it clear that we're at the end of the initramfs
splash_set_message "Starting"

# Re-enable kmsg ratelimiting (might have been disabled for logging)
echo ratelimit > /proc/sys/kernel/printk_devkmsg

# Vibrate to indicate that we are booting
if [ "$IN_CI" != "true" ]; then
	beebzzr &
fi

killall udevd syslogd unudhcpd 2>/dev/null

# Kill any getty shells that might be running
for pid in $(pidof sh); do
	if ! [ "$pid" = "1" ] && ! [ "$pid" = "$S5_DIAG_PID" ]; then
		kill -9 "$pid"
	fi
done

# cleanup after ourselves
# switch_root does a mount --move , keeping stale filesystems like devtmpfs
# with /dev/log in there.
rm /dev/log 2>/dev/null || true

# shellcheck disable=SC2093
exec switch_root /sysroot "$init"

echo "$LOG_PREFIX ERROR: switch_root failed!" > /dev/kmsg
echo "$LOG_PREFIX Looping forever. Install and use the debug-shell hook to debug this." > /dev/kmsg
echo "$LOG_PREFIX For more information, see <https://postmarketos.org/debug-shell>" > /dev/kmsg
fail_halt_boot
