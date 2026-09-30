#!/usr/bin/env python3
"""Derive the r26 initramfs script from r25.

r26 exists for one measured reason. On r25 the serial console finally produced
output, and every line of it was

    getty: bad speed: vt100

The real inittab carries

    ttyGS0::respawn:/sbin/getty -L ttyGS0 vt100

busybox getty takes a baud rate before the tty, so it reads ttyGS0 as the speed,
rejects it, then dies on vt100 as a speed. It never opens the port and never
prompts, and init respawns it forever. There is no working login program at the
far end, which is why every previous boot looked like a dead console.

r26 also stops naming USB services and starts matching them. r25 named one
service from a listing its own code produced, and the phone's screen on the r25
boot then showed a differently named one in the boot runlevel. A name taken
from a listing is still a guess, so r26 matches on shape instead and stubs
whatever it finds, after printing each one in full.

r26 also turns a kernel console on. The device tree's /chosen/bootargs already
asks for console=ttySAC2,115200, and ttySAC2 is an internal UART with nothing
wired to it, so all of the kernel's printk goes nowhere. The real system then
says so out loud:

    [pmOS-rd] Disabling console output again (use 'pmos.debug-shell' ...)

The kernel command line in the device tree is patched to ttyGS0 by the builder,
which is the only fix that survives, because a console named on the command line
is established by the kernel before anything userspace runs. The sysfs write
below is kept as well, but it is the belt to that pair of braces: the real
system can turn a console off again, and the r25 screen shows that it does.
"""
import pathlib

src = pathlib.Path("source/diagnostic-ramdisk-r25/init_2nd.sh")
dst = pathlib.Path("source/diagnostic-ramdisk-r26/init_2nd.sh")
s = src.read_text()
assert "s5usb_fix_getty" not in s, "r26 is already derived; start from r25"

STEPS = r'''# r26: the getty line in the real inittab is malformed, and that is the last
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

'''

NEUTRALISE = r'''# r26: find the USB services by what they are called, not by what a listing said
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
'''

anchor = "s5usb_neutralise() {"
assert s.count(anchor) == 1, "anchor is not unique, refusing to guess"
start = s.index(anchor)
# r25's neutralise runs to the "# Switch root" comment that follows it.
end = s.index("# Switch root", start)
s = s[:start] + NEUTRALISE + s[end:]
s = s.replace(anchor, STEPS + anchor, 1)

dst.write_text(s)
print("  r26 init_2nd.sh: %d bytes" % len(dst.read_text()))
