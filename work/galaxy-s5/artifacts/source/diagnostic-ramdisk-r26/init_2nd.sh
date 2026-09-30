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

# r25: the real cause, named at last.
#
# r24's dump of the real system, read off the phone before switch_root, listed
# the candidates and the first one is the answer:
#
#   /sysroot/etc/init.d/s5-usb-ncm
#
# That is an OpenRC service whose whole purpose is to put the gadget into ncm.
# It runs in the real system at a fixed point roughly 2 min 52 s after the
# console comes up, which is the same delay, to within a few seconds, across
# r20, r21, r23 and r24. Nothing about that number was ever the initramfs.
#
# Three revisions were spent on bind-mounting decoy files over the sysfs
# attributes instead, and all three binds failed:
#
#   WARNING could not bind over /sys/class/android_usb/android0/functions
#   WARNING could not bind over /sys/class/android_usb/android0/enable
#   WARNING could not bind over /sys/class/android_usb
#
# and the verification that was supposed to notice reported success anyway,
# because it read a real sysfs attribute that already said acm and had no way to
# tell a shadowed one from an unshadowed one. So r25 drops the binds entirely
# rather than keep a mechanism that does not work here, and goes after the
# script instead.
# r25: a root shell on the serial console, with no login.
#
# Carried over from r24, where it worked, and fixed on the way. The gadget serial
# port already has a getty in the real inittab -- "ttyGS0::respawn:/sbin/getty -L
# ttyGS0 vt100" -- so the console is a terminal and only a password stands
# between it and a shell. There is no way round that from out here: the real root
# filesystem is encrypted, TWRP cannot read it, and the one thing that could set
# or blank the password is writing to that same encrypted filesystem, which
# needs the shell first. So remove the authentication rather than the disk.
#
# Fail-open on purpose, and a diagnostic step rather than a configuration:
# anyone plugged into the USB port gets a root shell on the phone for as long as
# this image is on it. Removing it is one line, quoted at the end.
#
# The user's own password is never involved. Nothing is typed, nothing is
# stored, and no credential goes into the image, into CACHE or into any log.
s5usb_open_shell() {
	it=/sysroot/etc/inittab
	if [ ! -f "$it" ]; then
		echo "INFO: WARNING no $it, cannot add a login-free shell"
	else
		# Idempotent, because it demonstrably was not. r24 left this line in the
		# inittab twice -- the phone's own log reads
		#   s5usbconsole::respawn:/bin/sh -i|s5usbconsole::respawn:/bin/sh -i
		# and two respawn entries sharing an id fight each other for ttyGS0, so
		# the later one steals the terminal from the earlier. The install block
		# runs more than once in a boot, so the only correct question is
		# "is it already there", never "shall I add it".
		if grep -q '^s5usbconsole::' "$it" 2>/dev/null; then
			n=$(grep -c '^s5usbconsole::' "$it" 2>/dev/null)
			echo "INFO: a login-free shell is already in the inittab, $n entry(ies)"
			if [ "$n" -gt 1 ]; then
				# Collapse to one, keeping the first.
				tmp="$it.s5tmp"
				if awk -F: '/^s5usbconsole::/ { if (!seen) { seen=1; print } ; next } { print }' \
					"$it" > "$tmp" 2>/dev/null && cat "$tmp" > "$it" 2>/dev/null; then
					rm -f "$tmp"
					sync
					echo "INFO: collapsed them down to a single entry"
				else
					rm -f "$tmp"
					echo "INFO: WARNING could not collapse the duplicate entries"
				fi
			fi
		elif printf '%s\\n' 's5usbconsole::respawn:/bin/sh -i' >> "$it" 2>/dev/null; then
			sync
			echo "INFO: added a login-free root shell on the USB console"
		else
			echo "INFO: WARNING could not add the login-free shell"
		fi
		echo "INFO: to remove it later: sed -i '/s5usbconsole/d' /etc/inittab"
	fi

	# Belt and braces. The inittab route only works if the real init is busybox
	# init, and the evidence says it is not: r20 and r21 each installed an
	# inittab respawn line, verified it present and exact on disk, and the
	# keeper never wrote a line, which means PID 1 never read that file. r24
	# confirmed why: the real system has /etc/init.d, so it is OpenRC. Blank
	# the root password too, and the existing getty accepts a bare "root" and
	# Enter whichever init is in charge.
	#
	# An empty second field in /etc/shadow is not a stored password. The
	# original is kept as /etc/shadow.s5bak, so putting the phone back is a
	# rename and does not need the old password.
	sh=/sysroot/etc/shadow
	if [ ! -f "$sh" ]; then
		echo "INFO: WARNING no /etc/shadow, the real root password is unchanged"
	elif [ -f "$sh.s5bak" ]; then
		echo "INFO: /etc/shadow.s5bak already exists, shadow was blanked on an earlier boot"
	else
		if cp -p "$sh" "$sh.s5bak" 2>/dev/null || cp -f "$sh" "$sh.s5bak" 2>/dev/null; then
			echo "INFO: backed up /etc/shadow to /etc/shadow.s5bak"
		else
			echo "INFO: WARNING could not back up /etc/shadow, leaving it alone"
		fi
		# Only the root line, and only its password field. Every other account
		# and every other field is copied through byte for byte.
		if awk -F: 'BEGIN{OFS=":"} $1=="root"{$2=""; print; next} {print}' \
			"$sh" > "$sh.s5new" 2>/dev/null && cat "$sh.s5new" > "$sh" 2>/dev/null; then
			rm -f "$sh.s5new"
			sync
			echo "INFO: root password field is now empty; log in as root and press Enter"
		else
			rm -f "$sh.s5new"
			echo "INFO: WARNING could not blank the root password"
		fi
	fi
	echo "INFO: to restore it later: mv /etc/shadow.s5bak /etc/shadow"
}
s5usb_open_shell

# r26: find the USB services by what they are called, not by what a listing said
# they were called.
#
# r25 named one service, s5-usb-ncm, out of a list its own code had printed. The
# phone's screen on the r25 boot then showed a differently named service sitting
# in the boot runlevel, alongside a second one ending in the same three letters.
# A name copied out of a listing is still a guess about a phone nobody can read
# the filesystem of, and this boot cost a whole cycle to find out. So r26 matches
# on shape: anything in /etc/init.d whose name looks like it has business with
# USB is printed in full and then replaced with a stub.
#
# One list, used both to decide what to print and what to stub, so those two
# can never drift apart and leave something in scope that was never reported.
s5usb_usb_names() {
	for f in /sysroot/etc/init.d/*; do
		[ -f "$f" ] || continue
		name=${f##*/}
		low=$(echo "$name" | tr 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' 'abcdefghijklmnopqrstuvwxyz')
		case "$low" in
		*usb*|*ocn*|*ncm*|*acm*|*gadget*|*udc*|*dwc3*|*rndis*|*modem*|*cdc*|*tether*)
			echo "$name"
			;;
		esac
	done
}

# r26: take the USB services out of the real system's boot.
#
# What this is for, measured. From r18 onwards the serial console disappeared at
# a fixed point roughly 2 min 52 s into the real system, identically across four
# boots. r24's dump of the real system named the init scripts that mention USB,
# and r25 replaced one of them with a stub. The console then lasted 9 min 39 s
# instead of 2 min 52 s and came back as acm rather than ncm, so something was
# still re-touching the gadget and this was the right place to look rather than
# the whole answer.
#
# Every original is copied to CACHE and left beside itself as a .s5bak before
# anything is replaced, and the stub is a no-op that says so, so putting the
# phone back is a rename and needs nothing that was taken from the user.
# r26: the getty line in the real inittab is malformed, and that is the last
# thing standing between the serial console and a shell.
#
# The console finally spoke on r25, 26 times, and every line was the same:
#
#   getty: bad speed: vt100
#
# The real inittab carries
#
#   ttyGS0::respawn:/sbin/getty -L ttyGS0 vt100
#
# and busybox getty takes a baud rate before the tty, so it reads ttyGS0 as the
# speed, gives up on that, and then dies on vt100 as a speed. It never opens the
# port, never prints a banner and never prompts, and init respawns it, so the
# console produces that one line forever. Nothing is wrong with the port, the
# gadget or the password: there is simply no working login program at the far
# end.
#
# This also settles an old wrong conclusion. r20 and r21 each installed an
# inittab respawn line, found it present and exact on disk, and concluded that
# PID 1 never reads /etc/inittab. The phone has just proved that wrong. This
# getty is being respawned, so the file is read and respawn entries are honoured.
# Whatever stopped the r20/r21 keeper, it was not inittab.
#
# The repair is to give getty a speed it can accept. 115200 is conventional and
# is not meaningful for a USB CDC ACM line, since there is no physical baud rate
# to set; what matters is that the argument is present and parses as a number.
s5usb_fix_getty() {
	it=/sysroot/etc/inittab
	if [ ! -f "$it" ]; then
		echo "INFO: WARNING no $it, cannot repair the getty line"
		return 0
	fi
	# Show what is actually there before touching it, so this is never a blind
	# edit and the log is evidence either way.
	echo "INFO: getty lines in the real inittab, before:"
	if grep -n 'getty.*ttyGS' "$it" 2>/dev/null; then
		:
	else
		echo "INFO:   (none at all)"
	fi

	bak="$it.s5bak"
	if [ ! -f "$bak" ]; then
		if cp -p "$it" "$bak" 2>/dev/null || cp -f "$it" "$bak" 2>/dev/null; then
			echo "INFO: backed up the real inittab to /etc/inittab.s5bak"
		else
			echo "INFO: WARNING could not back up $it, not editing it"
			return 0
		fi
	fi

	# Rewrite only the ttyGS0 getty line, and only the first one. Every other
	# line, including the ttyS and tty1 entries and any second ttyGS0 getty,
	# is copied through byte for byte.
	ok=0
	if awk '
		/ttyGS0/ && /getty/ && !done {
			print "ttyGS0::respawn:/sbin/getty -L 115200 ttyGS0 vt100"
			print "# r26 replaced the line above; it read: " $0
			done = 1
			next
		}
		{ print }
	' "$bak" > "$it.s5new" 2>/dev/null && [ -s "$it.s5new" ] &&
		cat "$it.s5new" > "$it" 2>/dev/null; then
		ok=1
	fi
	rm -f "$it.s5new"
	if [ "$ok" -eq 1 ]; then
		sync
		echo "INFO: getty lines in the real inittab, after:"
		if grep -n 'getty.*ttyGS' "$it" 2>/dev/null; then
			:
		else
			echo "INFO:   (none at all)"
		fi
		# Whole-line match, not a substring: the comment recording the old line
		# would otherwise satisfy a looser test and let a failed repair report
		# itself as a success.
		if grep -q '^ttyGS0::respawn:/sbin/getty -L 115200 ttyGS0 vt100$' "$it" 2>/dev/null; then
			echo "INFO: repaired the getty line; a login prompt is now possible"
		else
			echo "INFO: WARNING the repair did not land, the console will stay mute"
		fi
	else
		cp -f "$bak" "$it" 2>/dev/null
		sync
		echo "INFO: WARNING could not rewrite $it, restored it unchanged"
	fi
	echo "INFO: to undo the getty repair: mv /etc/inittab.s5bak /etc/inittab"
}
s5usb_fix_getty

# r26: turn a kernel console on as well.
#
# The builder patches the device tree's /chosen/bootargs from console=ttySAC2 to
# console=ttyGS0, which is the part that matters, because a console named on the
# kernel command line is set up before any userspace runs. This write is the
# second belt, in case the tree patch is ever dropped, and it is strictly best
# effort: the real system announces that it turns the console back off.
#
#   [pmOS-rd] Disabling console output again (use 'pmos.debug-shell' ...)
#
# so anything this does, the real system is entitled to undo. Every failure here
# is reported and none of them stop the boot.
s5usb_console_on() {
	act=/sys/class/tty/console/active
	if [ ! -e "$act" ]; then
		echo "INFO: WARNING no $act, cannot enable a console"
		return 0
	fi
	before=$(cat "$act" 2>/dev/null)
	echo "INFO: console/active was [$before]"
	if echo ttyGS0 > "$act" 2>/dev/null; then
		echo "INFO: console/active is now [$(cat "$act" 2>/dev/null)]"
		echo "INFO: the real system's kernel messages should reach ttyGS0"
	else
		echo "INFO: WARNING could not point the console at ttyGS0"
	fi
}
s5usb_console_on

s5usb_neutralise() {
	initd=/sysroot/etc/init.d
	if [ ! -d "$initd" ]; then
		echo "INFO: WARNING no $initd, nothing to neutralise"
		return 0
	fi

	# /cache is NOT CACHE in this initramfs. It is a tmpfs that goes away with
	# the old root, which is how r24 reported three successful writes to
	# /cache/codex-s5-diagnostics/r24 and left nothing behind at all. s5diag
	# mounts the CACHE device at /mnt/s5diag; do the same, and say plainly
	# which one is being used.
	out=""
	for dev in /dev/mmcblk0p19 /dev/block/mmcblk0p19 /dev/mmcblk0p20; do
		[ -b "$dev" ] || continue
		mkdir -p /mnt/s5r25 2>/dev/null || continue
		if mount -t ext4 -o rw,noatime,nodev,nosuid,noexec "$dev" /mnt/s5r25 2>/dev/null; then
			if mkdir -p /mnt/s5r25/codex-s5-diagnostics/r26 2>/dev/null; then
				out=/mnt/s5r25/codex-s5-diagnostics/r26
			fi
			break
		fi
	done
	if [ -n "$out" ]; then
		echo "INFO: CACHE mounted at /mnt/s5r25, originals go to $out"
	else
		echo "INFO: WARNING CACHE would not mount, originals stay beside the originals"
	fi

	names=$(s5usb_usb_names)
	if [ -z "$names" ]; then
		echo "INFO: WARNING no USB service matched, so acm can still be taken"
		echo "INFO: everything in $initd, so this can be checked by eye:"
		ls -l "$initd" 2>&1 | sed 's/^/INFO:   /'
		return 0
	fi
	echo "INFO: USB services in the real system: $names"

	# Print each one in full. The initramfs log is the artefact that has come
	# back reliably on every single boot, so it is the channel to use when in
	# doubt about the other one, and this is the only way to see what a service
	# does without a shell on the real system.
	#
	# Bodies first, and the runlevel entries only for the names in scope. r25
	# printed the whole of /etc/init.d first, which is over a hundred lines, and
	# the log stopped dead partway through the first service body at 5370 bytes.
	# The part that mattered was the part that got cut.
	{
		for name in $names; do
			echo "=== r26: BEGIN /etc/init.d/$name ==="
			cat "$initd/$name" 2>&1
			echo "=== r26: END /etc/init.d/$name ==="
		done
		echo "=== r26: runlevel entries for the names in scope ==="
		for lvl in /sysroot/etc/runlevels/*; do
			[ -d "$lvl" ] || continue
			for name in $names; do
				[ -L "$lvl/$name" ] && echo "  $lvl/$name -> $(readlink "$lvl/$name" 2>/dev/null)"
			done
		done
		echo "=== r26: the rest of /etc/init.d, $(ls "$initd" 2>/dev/null | wc -l) names ==="
		ls "$initd" 2>&1
	} 2>&1

	n=0
	links=0
	for name in $names; do
		f="$initd/$name"
		[ -f "$f" ] || continue

		# Back up first, always, and never over a copy that is already good.
		if [ ! -f "$f.s5bak" ]; then
			if cp -p "$f" "$f.s5bak" 2>/dev/null || cp -f "$f" "$f.s5bak" 2>/dev/null; then
				[ -n "$out" ] && cp -f "$f" "$out/$name" 2>/dev/null
				echo "INFO: kept the original of $name as $name.s5bak"
			else
				echo "INFO: WARNING could not back up $f, leaving it alone"
				continue
			fi
		else
			[ -n "$out" ] && cp -f "$f" "$out/$name" 2>/dev/null
			echo "INFO: $name was already stubbed on an earlier boot"
		fi

		# Replace the body with a no-op that says so. OpenRC runs whatever is
		# at that path, so replacing the file is enough on its own; the links
		# below are the belt.
		cat > "$f" <<'STUB'
#!/sbin/openrc-run
# Replaced by r26. The USB services in this directory were reconfiguring the
# gadget partway through every boot, which is what took the serial console away
# and then brought it back on a timer. The original is beside this file with a
# .s5bak suffix, and a copy is in the r26 dump on CACHE.
echo "this USB service is disabled by r26 and does nothing" >/dev/kmsg
STUB
		chmod 0755 "$f" 2>/dev/null

		# And take the runlevel links away.
		#
		# -L, not -e, and this is not a style point. These links point at
		# /etc/init.d/..., which does not exist in this initramfs, so -e is
		# false for every one of them. r25 tested with -e, removed nothing,
		# said nothing about removing nothing, and left the link that is the
		# whole reason OpenRC starts the service sitting exactly where it was.
		for link in /sysroot/etc/runlevels/*/"$name"; do
			if [ -L "$link" ]; then
				if rm -f "$link" 2>/dev/null; then
					links=$((links + 1))
					echo "INFO: removed the runlevel link ${link#/sysroot}"
				else
					echo "INFO: WARNING could not remove the runlevel link $link"
				fi
			fi
		done
		n=$((n + 1))
	done

	# Say plainly what happened, and admit it if nothing did. r22, r23 and r24
	# each printed a success line while the thing they claimed to have done
	# had not, and that is the failure mode worth designing against.
	echo "INFO: stubbed $n USB service(s) and removed $links runlevel link(s)"
	if [ "$n" -gt 0 ] && [ "$links" -eq 0 ]; then
		echo "INFO: WARNING nothing was unlinked, so OpenRC may still start them"
	elif [ "$n" -eq 0 ]; then
		echo "INFO: WARNING nothing was stubbed, so acm can still be taken"
	else
		echo "INFO: to restore them: for f in $initd/*.s5bak; do mv "$f" "${f%.s5bak}"; done"
	fi
	[ -n "$out" ] && echo "INFO: originals and the dump are at $out"
}
s5usb_neutralise
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
