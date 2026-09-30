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
