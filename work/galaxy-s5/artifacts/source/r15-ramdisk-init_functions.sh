#!/bin/busybox ash
# This file will be in /init_functions.sh inside the initramfs.

# NOTE!!! The file is sourced again in init_2nd.sh, avoid
# clobbering variables by not setting them if they have
# a value already!
PMOS_BOOT="${PMOS_BOOT:-}"
PMOS_ROOT="${PMOS_ROOT:-}"
SUBPARTITION_DEV="${SUBPARTITION_DEV:-}"
SUBPARTITION_LOOP="${SUBPARTITION_LOOP:-}"

CONFIGFS="/sys/kernel/config/usb_gadget"
CONFIGFS_ACM_FUNCTION="acm.usb0"
CONFIGFS_MASS_STORAGE_FUNCTION="mass_storage.0"
HOST_IP="${unudhcpd_host_ip:-172.16.42.1}"

deviceinfo_getty="${deviceinfo_getty:-}"
deviceinfo_name="${deviceinfo_name:-}"
deviceinfo_codename="${deviceinfo_codename:-}"
deviceinfo_create_initfs_extra="${deviceinfo_create_initfs_extra:-}"
deviceinfo_no_framebuffer="${deviceinfo_no_framebuffer:-}"
deviceinfo_rootfs_image_sector_size="${deviceinfo_rootfs_image_sector_size:-}"

# Default to no splash unless set on the kernel cmdline. Plymouth will not
# display a splash if this param is missing from the cmdline
nosplash="${nosplash:-y}"

# Does word start with prefix?
startswith() {
	local word="$1" prefix="$2"

	case "$word" in
		$prefix*)
			return 0
			;;
	esac

	return 1
}

# Does word end with suffix?
endswith() {
	local word="$1" suffix="$2"

	case "$word" in
		*$suffix)
			return 0
			;;
	esac

	return 1
}

# Parse individual items from the cmdline and take appropriate
# action (e.g. set variables).
parse_cmdline_item() {
	local key="$1" value="$2"

	# For information on what each cmdline argument does, please refer
	# to https://wiki.postmarketos.org/wiki/Initramfs#Kernel_cmdline
	case "$key" in
		pmos.boot | pmos_boot)
			boot_path="$value"
			;;
		pmos.boot_uuid | pmos_boot_uuid)
			boot_uuid="$value"
			;;
		pmos.bootchart2 | PMOS_BOOTCHART2)
			bootchart2=y
			;;
		pmos.debug-shell)
			# used by init_2nd.sh
			# shellcheck disable=SC2034
			debug_shell=y
			;;
		pmos.force-partition-resize | PMOS_FORCE_PARTITION_RESIZE)
			# used by init_functions_2nd.sh which is sourced
			# after this script.
			# shellcheck disable=SC2034
			force_partition_resize=y
			;;
		splash)
			nosplash=n
			;;
		pmos.root | pmos_root)
			root_path="$value"
			;;
		pmos.root_uuid | pmos_root_uuid)
			root_uuid="$value"
			;;
		pmos.rootfsopts | pmos_rootfsopts)
			# Prepend a comma since this will be appended to
			# "rw" when passed to mount
			rootfsopts=",$value"
			;;
		pmos.stowaway)
			stowaway=y
			;;
		pmos.usb-storage)
			usb_storage="$value"
			;;
		rd.info)
			# used by init_functions_2nd.sh which is sourced
			# after this script.
			# shellcheck disable=SC2034
			log_info=y
			;;
		cryptkey)
			# shellcheck disable=SC2034
			cryptkey="$value"
			;;
		[![:alpha:]_]* | [[:alpha:]_]*[![:alnum:]_]*)
			# invalid shell variable, ignore it
			;;
		*)
			# valid shell variable
			# ignore these since we get all kinds of weird
			# arguments from quirky bootloaders and we really
			# don't want to deal with that here. Add options
			# explicitly above instead.
			;;
	esac
}

process_cmdline_param() {
	# $1: key (the cmdline parameter)
	# $2: value (after the =)
	local key="$1" value="$2"

	# maybe unquote the value
	# busybox ash supports string indexing
	# shellcheck disable=SC3057
	if startswith "$value" "[\"']" && endswith "$value" "${value:0:1}"; then
		value="${value#?}" value="${value%?}"
	fi

	parse_cmdline_item "$key" "$value"
}

# Parse the kernel cmdline and configure the environment
parse_cmdline() {
	local cmdline word quoted key value

	# Disable globbing so we don't accidentally expand
	# cmdline options into paths
	set -f
	read -r cmdline
	# shellcheck disable=SC2086
	set -- $cmdline
	set +f

	# Walk over each cmdline argument
	for word; do
		# Handle quoted values
		if [ -n "$quoted" ]; then
			value="$value $word"
		else
			case "$word" in
				# Easy case: key=value
				*=*)
					key="${word%%=*}"
					value="${word#*=}"

					if startswith "$value" "[\"']"; then
						# busybox ash supports string indexing
						# shellcheck disable=SC3057
						quoted="${value:0:1}"
					fi
					;;
				# A comment (only used for unit tests)
				'#'*)
					break
					;;
				# A key without a value
				*)
					key="$word"
					;;
			esac
		fi

		# If inside a quoted string, check if $value contains the closing quote
		if [ -n "$quoted" ]; then
			if endswith "$value" "$quoted"; then
				unset quoted
			else
				# Otherwise continue reading words
				continue
			fi
		fi

		process_cmdline_param "$key" "$value"
		unset key value
	done

	if [ -n "$key" ]; then
		process_cmdline_param "$key" "$value"
	fi

	# Also set some options from deviceinfo variables

	if [ "$deviceinfo_no_framebuffer" = "true" ]; then
		nosplash=y
	fi
}

# Redirect stdout/stderr to the log file, as well as to the kernel via
# syslog. Additionally, if nosplash is set and there are no active
# consoles, try to be helpful by logging to tty0 and the devices serial
# port.
setup_log() {
	local console
	local log_targets
	console="$(cat /sys/devices/virtual/tty/console/active)"
	log_targets="/pmOS_init.log"

	# If we have an active console, the kernel will be logging there.
	if [ -n "$console" ] ; then
		exec 3>&1 4>&2
	else
		# Setting console=null is a trick used on quite a few pmOS devices. However it is generally a really
		# bad idea since it makes it impossible to debug kernel panics, and it makes our job logging in the
		# initramfs a lot harder. We ban this in pmaports but some (usually android) bootloaders like to add it
		# anyway. We ought to have some special handling here to use /dev/zero for stdin instead
		# to avoid weird bugs in daemons that read from stdin (e.g. syslog)
		# See related: https://gitlab.postmarketos.org/postmarketOS/pmaports/-/issues/2989
		console="/dev/$(echo "$deviceinfo_getty" | cut -d';' -f1)"
		if [ -e "$console" ]; then
			log_targets="$log_targets $console"
		fi

		# If nosplash is set but there's no active console, let's try to be helpful by at least
		# logging the initramfs output to the consoles we can find.
		# TODO: This could be further improved by reading /dev/kmsg and outputting it as well which
		# might help with debugging on bootloaders that do force console=null
		if [ "$nosplash" = "y" ]; then
			echo "Splash is disabled but no consoles are active, trying to log somewhere useful!" \
				| tee "$console" > /dev/tty0
			# Log to tty0 as well as the serial port we may have found above
			log_targets="$log_targets /dev/tty0"
		fi
	fi

	# Disable kmsg ratelimiting for userspace (it gets re-enabled again before switch_root)
	echo on > /proc/sys/kernel/printk_devkmsg

	# Spawn syslogd to log to the kernel
	# syslog will try to read from stdin over and over which can pin a cpu when stdin is /dev/null
	# By connecting /dev/zero to stdin/stdout/stderr, we make sure that syslogd
	# isn't blocked when a console isn't available.
	syslogd -K < /dev/zero >/dev/zero 2>&1

	# Log to ramoops as well
	if [ -e "/dev/pmsg0" ]; then
		log_targets="$log_targets /dev/pmsg0"
	fi

	# Redirect to a subshell which outputs to the logfile as well
	# as to the kernel ringbuffer and pstore (if available).
	# Process substitution is technically non-POSIX, but is supported by busybox
	# We are intentionally word-splitting $log_targets into multiple arguments.
	# shellcheck disable=SC3001,SC2086
	exec > >(tee $log_targets | logger -t "$LOG_PREFIX" -p user.info) 2>&1
}

info() {
	if [ "$log_info" != "y" ]; then
		return
	fi

	echo "$@"
}

mount_proc_sys_dev() {
	# mdev
	mount -t proc -o nodev,noexec,nosuid proc /proc || echo "Couldn't mount /proc"
	mount -t sysfs -o nodev,noexec,nosuid sysfs /sys || echo "Couldn't mount /sys"
	mount -t devtmpfs -o mode=0755,nosuid dev /dev || echo "Couldn't mount /dev"
	mount -t tmpfs -o nosuid,nodev,mode=0755 run /run || echo "Couldn't mount /run"

	modprobe libcomposite 2>/dev/null || true
	mount -t configfs -o nodev,noexec,nosuid configfs "$(dirname "$CONFIGFS")"

	# /dev/pts (needed for telnet)
	mkdir -p /dev/pts
	mount -t devpts devpts /dev/pts

	# This is required for process substitution to work (as used in setup_log())
	ln -s /proc/self/fd /dev/fd
}

setup_firmware_path() {
	# Add the postmarketOS-specific path to the firmware search paths.
	# This should be sufficient on kernel 3.10+, before that we need
	# the kernel calling udev (and in our case /usr/lib/firmwareload.sh)
	# to load the firmware for the kernel.
	local sysfs_dir
	sysfs_dir=/sys/module/firmware_class/parameters/path
	if ! [ -e "$sysfs_dir" ]; then
		echo "Kernel does not support setting the firmware image search path. Skipping."
		return
	fi
	# shellcheck disable=SC3037
	echo -n /lib/firmware/postmarketos >$sysfs_dir
}

# shellcheck disable=SC3043
load_modules() {
	local file="$1"
	local modules="$2"
	[ -f "$file" ] && modules="$modules $(grep -v ^\# "$file")"

	if [ -z "$modules" ]; then
		return
	fi
	# shellcheck disable=SC2086
	modprobe -a $modules
}

setup_mdev() {
	# Start mdev daemon
	mdev -d
}

jump_init_2nd() {
	if ! [ -e /init_2nd.sh ]; then
		return
	fi

	echo "  ❬❬ PMOS STAGE 2 ❭❭"
	exec /init_2nd.sh
}

get_uptime_seconds() {
	# Get the current system uptime in seconds - ignore the two decimal places.
	awk -F '.' '{print $1}' /proc/uptime
}

setup_dynamic_partitions() {
	command -v make-dynpart-mappings > /dev/null || return
	attempt_start=$(get_uptime_seconds)
	wait_seconds=10
	slot_number=0
	for super_partition in $1; do
		# Wait for mdev
		echo "Waiting for super partition $super_partition..."
		while [ ! -b "$super_partition" ]; do
			if [ "$(get_uptime_seconds)" -ge $(( attempt_start + wait_seconds )) ]; then
				echo "ERROR: Super partition $super_partition failed to show up!"
				return;
			fi
			sleep 0.1
		done
		make-dynpart-mappings "$super_partition" "$slot_number"
		slot_number=$(( slot_number + 1 ))
	done
}

# >>> dm-bypass: helpers (begin)
# Device-mapper access to the nested partitions of a whole-disk installation.
#
# With this kernel the loop device that mount_subpartitions() normally uses
# cannot read LBA 0 of the internal mmc partition: every attempt ends in
# "Buffer I/O error on device loop0, logical block 0" plus "loop0: unable to
# read partition table", while the mmc stack itself never reports an error.
# The very same subpartitions read fine under recovery, where kpartx builds
# plain device-mapper linear tables over the identical extents:
#
#   0 497664   linear <partition> 2048
#   0 24346624 linear <partition> 499712
#
# So create those tables here too, and keep losetup as the fallback.
#
# Nothing is remapped unless every guard matches: a real partition of the
# internal mmc disk addressed in 512 byte sectors, an MBR with exactly two
# Linux entries that lie inside the partition, do not overlap and leave the
# first megabyte alone, a mapping whose sysfs entry confirms the requested
# size, name and backing device, and filesystems that identify as pmOS_boot
# (ext2) and pmOS_root (ext4). Any mismatch removes the mappings again and
# lets the losetup path run exactly as before.
#
# The partition paths are pinned here instead of being looked up afterwards,
# because util-linux blkid only searches device-mapper devices that got major
# 253. This kernel hands them 254, so find_partition() would not see the
# partitions created here even though blkid identifies them fine by name.

# Mappings created by the current dm_attach_subpartitions() call, so that a
# failed attempt can be undone completely.
DM_SUBPARTITIONS=""

# Set by dm_attach_one() to the usable path and role of the mapping it built.
DM_SUBPARTITION_NODE=""
DM_SUBPARTITION_ROLE=""

# Make sure /dev/mapper/control exists. The control device is a misc device
# that devtmpfs exposes as /dev/device-mapper on this kernel, and the
# initramfs has no mdev rule for it.
dm_control_node() {
	[ -e /dev/mapper/control ] && return 0
	mkdir -p /dev/mapper || return 1
	local minor
	# device-mapper is the name the kernel registers in /proc/misc; accept
	# only that one, and only when it yields a single bare number, so a
	# device that is not device-mapper can never be turned into a node.
	minor="$(awk '$2 == "device-mapper" { print $1 }' /proc/misc 2>/dev/null)"
	[ -n "$minor" ] || return 1
	case "$minor" in
		*[!0-9]*) return 1 ;;
	esac
	busybox mknod -m 600 /dev/mapper/control c 10 "$minor" 2>/dev/null
}

# Print the sysfs directory of the device-mapper device called $1, so its
# size, name and backing device can be checked. device-mapper is not
# registered below /sys/class/block/mapper, so resolve it by device number.
dm_sysfs_path() {
	local major minor path
	major="$(dmsetup info -c --noheadings -o major "$1" 2>/dev/null)"
	minor="$(dmsetup info -c --noheadings -o minor "$1" 2>/dev/null)"
	[ -n "$major" ] && [ -n "$minor" ] || return 1
	path="$(readlink -f "/sys/dev/block/$major:$minor" 2>/dev/null)"
	[ -d "$path" ] || return 1
	case "$path" in
		*/dm-[0-9]*) echo "$path" ;;
		*) return 1 ;;
	esac
}

# Print the value of the blkid token named in $2, taken from the blkid output
# in $1. "LABEL=\"pmOS_root\" ... TYPE=\"ext4\"" therefore yields pmOS_root
# and ext4 regardless of the order the tokens appear in.
dm_blkid_token() {
	echo "$1" | tr ' ' '\n' | sed -n "s/^$2=\"\(.*\)\"\$/\1/p"
}

# Print "<index> <start> <sectors>" for every MBR entry in the fdisk output on
# stdin that has the expected shape, for the device given in $1. busybox
# fdisk prints the device path with a pN suffix in the first column and
# leaves the boot flag column empty for unflagged entries, so the numbers are
# counted from the end of the line: $(NF-3) is the sector count and $(NF-5)
# the first sector. Anything that does not match is ignored on purpose, so an
# unexpected table means no entries and the caller falls back to losetup.
dm_mbr_entries() {
	awk -v dev="$1" '
		NF >= 8 &&
		length($1) == length(dev) + 2 &&
		substr($1, 1, length(dev) + 1) == dev "p" &&
		$NF == "Linux" && $(NF-1) == "83" &&
		$(NF-2) ~ /^[0-9]+(\.[0-9]+)?[KMGTP]?$/ &&
		$(NF-3) ~ /^[0-9]+$/ && $(NF-4) ~ /^[0-9]+$/ && $(NF-5) ~ /^[0-9]+$/ {
			print substr($1, length(dev) + 2) + 0, $(NF-5) + 0, $(NF-3) + 0
		}
	'
}

# Remove the mappings named in $1 along with their device nodes.
dm_remove_subpartitions() {
	local name
	for name in $1; do
		dmsetup remove "$name" 2>/dev/null
		rm -f "/dev/mapper/$name"
	done
	DM_SUBPARTITIONS=""
}

# Create the mapping <base>p<index> over "<start>+<sectors>" sectors of
# $1, then verify it. On success DM_SUBPARTITION_NODE holds a usable path and
# DM_SUBPARTITION_ROLE is either "boot" or "root"; on failure the mapping is
# left registered in DM_SUBPARTITIONS for the caller's cleanup.
dm_attach_one() {
	local partition="$1" base="$2" index="$3" start="$4" sectors="$5"
	local name="${2}p${3}" sysfs major minor id label type

	DM_SUBPARTITION_NODE=""
	DM_SUBPARTITION_ROLE=""

	# Never take over a device that is already there.
	[ -e "/dev/mapper/$name" ] && return 1
	dmsetup create "$name" --table "0 $sectors linear $partition $start" || return 1
	DM_SUBPARTITIONS="$DM_SUBPARTITIONS $name"

	# The mapping has to be exactly the requested extent of the parent.
	sysfs="$(dm_sysfs_path "$name")" || return 1
	[ "$(cat "$sysfs/size" 2>/dev/null)" = "$sectors" ] || return 1
	[ "$(cat "$sysfs/dm/name" 2>/dev/null)" = "$name" ] || return 1
	[ -e "$sysfs/slaves/$base" ] || return 1

	# dmsetup mknodes should have made the node; build it from the device
	# number if it did not, because mdev has no rule for these either.
	if [ ! -e "/dev/mapper/$name" ]; then
		major="$(dmsetup info -c --noheadings -o major "$name" 2>/dev/null)"
		minor="$(dmsetup info -c --noheadings -o minor "$name" 2>/dev/null)"
		[ -n "$major" ] && [ -n "$minor" ] || return 1
		busybox mknod -m 600 "/dev/mapper/$name" b "$major" "$minor" 2>/dev/null || return 1
	fi
	[ -b "/dev/mapper/$name" ] || return 1

	# Only the two filesystems of the recorded installation are accepted.
	id="$(blkid "/dev/mapper/$name" 2>/dev/null)"
	label="$(dm_blkid_token "$id" LABEL)"
	type="$(dm_blkid_token "$id" TYPE)"
	if [ "$label" = "pmOS_boot" ] && [ "$type" = "ext2" ]; then
		DM_SUBPARTITION_ROLE="boot"
	elif [ "$label" = "pmOS_root" ] && [ "$type" = "ext4" ]; then
		DM_SUBPARTITION_ROLE="root"
	else
		return 1
	fi
	DM_SUBPARTITION_NODE="/dev/mapper/$name"
}

# Try to expose the nested partitions of the partition given in $1 as
# device-mapper linear mappings and pin PMOS_BOOT and PMOS_ROOT to them.
# Returns 0 when both are pinned, and non-zero with everything it created
# removed again when the device, the table or the filesystems do not match.
dm_attach_subpartitions() {
	local partition="$1"
	local base size entries count index start sectors node role
	local found_boot="" found_root=""

	DM_SUBPARTITIONS=""

	# Only a real partition of the internal mmc disk may be remapped, and
	# only if it is addressed in 512 byte sectors, which is what a
	# device-mapper table counts in.
	base="${partition##*/}"
	[ -e "/sys/class/block/$base/partition" ] || return 1
	case "$(readlink -f "/sys/class/block/$base")" in
		*/mmcblk*/*) ;;
		*) return 1 ;;
	esac
	[ "$(blockdev --getss "$partition" 2>/dev/null)" = "512" ] || return 1

	size="$(cat "/sys/class/block/$base/size" 2>/dev/null)"
	[ -n "$size" ] || return 1

	# Exactly two usable entries, ascending, without overlap, reaching no
	# further than the end of the partition.
	entries="$(fdisk -l "$partition" 2>/dev/null | dm_mbr_entries "$partition")"
	count="$(echo "$entries" | grep -c .)"
	if [ "$count" -ne 2 ]; then
		echo "Device-mapper subpartitions skipped for $partition: $count MBR entries"
		return 1
	fi
	if ! echo "$entries" | awk -v psize="$size" '
		{
			if (seen && $2 < prev) { bad = 1 }
			if ($2 < 2048 || $3 < 1 || $2 + $3 > psize) { bad = 1 }
			prev = $2 + $3
			seen = 1
		}
		END { exit bad || !seen ? 1 : 0 }
	'; then
		echo "Device-mapper subpartitions skipped for $partition: MBR entries out of range"
		return 1
	fi

	# Everything from here on may leave a mapping behind, so every failure
	# has to go through the cleanup below.
	dm_control_node || return 1

	while read -r index start sectors; do
		[ -n "$index" ] || continue
		if ! dm_attach_one "$partition" "$base" "$index" "$start" "$sectors"; then
			echo "Device-mapper mapping ${base}p${index} rejected, using losetup instead"
			dm_remove_subpartitions "$DM_SUBPARTITIONS"
			return 1
		fi
		node="$DM_SUBPARTITION_NODE"
		role="$DM_SUBPARTITION_ROLE"
		if [ "$role" = "boot" ]; then
			[ -z "$found_boot" ] || { dm_remove_subpartitions "$DM_SUBPARTITIONS"; return 1; }
			found_boot="$node"
		else
			[ -z "$found_root" ] || { dm_remove_subpartitions "$DM_SUBPARTITIONS"; return 1; }
			found_root="$node"
		fi
	done <<DM_MBR_ENTRIES
$entries
DM_MBR_ENTRIES

	# One boot and one root filesystem, and nothing pinned already.
	if [ -z "$found_boot" ] || [ -z "$found_root" ] || [ -n "$PMOS_BOOT" ] || [ -n "$PMOS_ROOT" ]; then
		dm_remove_subpartitions "$DM_SUBPARTITIONS"
		return 1
	fi
	PMOS_BOOT="$found_boot"
	PMOS_ROOT="$found_root"
	echo "Mounted subpartitions of $partition with device-mapper: $PMOS_BOOT $PMOS_ROOT"
	return 0
}
# <<< dm-bypass: helpers (end)
# >>> s5screen: helpers (begin)
# Default s5keys to "n" so the halt below cannot fire.
#
# This is the only way past the halt on this device, and it has to be set here
# rather than on the kernel command line because the bootloader ignores the BOOT
# image's cmdline field: the r14 kernel log shows the complete 850-byte command
# line the kernel received, and it contains neither "quiet" nor
# "buildvariant=eng", which are the entire contents of that field. Everything
# the kernel sees comes from the device tree's /chosen/bootargs, so an s5keys=n
# written into the boot header is silently discarded.
#
# ${s5keys:-n} rather than a bare assignment so that parse_cmdline, which runs
# after this file is sourced, still wins if a command line ever does carry the
# key. That keeps the documented override working for a future device where the
# bootloader does pass the header through.
#
# What is given up: holding volume-up plus left-shift no longer halts the boot
# and dumps logs, because the real volume-key driver (gpio_keys.16) reports
# KEY_VOLUMEUP held on every sample with nothing pressed, so that check fired on
# every boot and init_2nd.sh blocked at check_keys, never reaching
# mount_subpartitions. The volume-down plus left-control debug shell entry in
# check_keys is above this guard and uses raw iskey, so it still works: there is
# still a way to stop and inspect a bad boot. The underlying phantom on
# gpio_keys.16 is unfixed and remains the real bug.
s5keys="${s5keys:-n}"
# Paint the boot state onto the panel, and stop trusting a single iskey sample.
# Both are diagnostic-only and must never be able to stop a boot.
#
# s5status <state> shows one of: booting, dmok, dmno, loopfail, keys, booted.
# The kernel in this image is built with CONFIG_FRAMEBUFFER_CONSOLE=y, so the
# panel is no longer black and the real system's console is visible. That is
# the fix; this is the part console text cannot do. The console interleaves
# kernel and initramfs output, scrolls, and is gone once switch_root runs, so
# "which of the six things happened" is only ever readable as a single painted
# word that cannot scroll away or be missed. It also covers the window before
# the fb device registers, where there is still no console at all.
# s5screen mmaps /dev/fb0 and paints the state itself: on this device the
# framebuffer has to be written through mmap, because the fbdev accepts a plain
# write(2) but consumes nothing, and rejects msync(2) with EINVAL. Every
# failure inside s5screen is swallowed and it always exits 0, so this can cost
# the screen but never a boot.
# Set nos5screen=y on the kernel command line to switch it off entirely.
# Usage: s5status <state>
# Sets: (none)
# Returns: 0
s5status() {
	[ "$nos5screen" = "y" ] && return 0
	s5screen "$1" >/dev/null 2>&1
	return 0
}

# Require the given keys to still be reported as held on four consecutive
# samples a second apart before believing that anybody is pressing them, and
# ignore input devices that synthesise keys rather than reading real ones.
#
# Two separate problems are handled here.
#
# First, iskey aggregates libevdev state over every /dev/input/event* and cannot
# say which device answered. On this device two nodes advertise KEY_VOLUMEUP:
# gpio_keys.16, which is the real volume key, and "Headset" (arizona-extcon),
# which synthesises headset remote key events and can emit one while the
# headphone detect pin is still settling early in boot. Nothing here advertises
# KEY_LEFTSHIFT, so the "hold left shift and volume up to fail the boot" check
# can only be triggered by volume-up, and a synthesised volume-up therefore
# halts the boot on its own. That is what killed boot 6: check_keys called
# fail_halt_boot at about 5.15s, before mount_subpartitions was reached, and
# with no framebuffer console there was nothing on screen to say why. s5iskey
# asks the same question but skips the device whose name says it synthesises
# keys, and prints which device did answer, so a log records the real source.
#
# Second, a single sample is not enough. A real keypress is held far longer than
# the three seconds this waits, so requiring persistence keeps the documented
# behaviour while ignoring momentary noise. This has still been observed to fire
# on one boot and not the next on the same image, so on a positive every input
# device name is also dumped: that is what separates "a person is holding volume
# up" from "a driver is stuck asserting it", and it is not recoverable after
# the fact. Set s5keys=n on the kernel command line to skip the check entirely.
# Usage: keys_still_held KEY [KEY...]
# Sets: (none)
# Returns: 0 if held on every sample, 1 if any sample disagreed
keys_still_held() {
	local i out
	[ $# -gt 0 ] || return 1
	[ "$s5keys" = "n" ] && { echo "INFO: s5keys=$s5keys, ignoring held key $*"; return 1; }
	i=1
	while [ "$i" -le 4 ]; do
		# s5iskey, not iskey: it skips the synthesising headset device and
		# names the device that answered. Its stdout is captured rather than
		# left to the tee, so the device name is guaranteed to reach the log
		# on the same path as every other line here.
		if out="$(s5iskey --verbose "$@" 2>&1)"; then
			[ -n "$out" ] && echo "$out"
		else
			echo "INFO: keys $* not held on sample $i/4, continuing boot"
			return 1
		fi
		[ "$i" -lt 4 ] && sleep 1
		i=$((i + 1))
	done
	echo "INFO: keys $* held on all 4 samples, treating as a real keypress"
	# Which input devices exist at all. The H: capability bitmaps are 192 hex
	# digits each and would bury the log for no gain, since s5iskey has already
	# named the one that answered.
	if [ -e /proc/bus/input/devices ]; then
		echo "INFO: input devices: $(grep -c '^N: ' /proc/bus/input/devices 2>/dev/null) total"
		grep '^N: ' /proc/bus/input/devices 2>/dev/null | sed 's/^/INFO:   /'
	else
		echo "INFO: /proc/bus/input/devices is not readable"
	fi
	echo "INFO: evdev nodes: $(ls /dev/input 2>/dev/null | tr '\n' ' ')"
	return 0
}

# Give the installed system a login prompt on the USB gadget serial port.
#
# This kernel has CONFIG_USB_F_ACM=y already, so no rebuild is needed for a
# serial console. The gadget is only created when USB networking cannot bind a
# UDC, and on this device it never can: the ncm, rndis and mass_storage
# functions are all absent, so "Couldn't write new UDC" is the normal outcome
# and /dev/ttyGS0 is the only way in. That is exactly the path debug_shell
# takes, which is why a serial login worked during the halt.
#
# Called from mount_root_partition(), so /sysroot is already mounted read-write
# and the installed system's own configuration can be edited before
# switch_root. Every step is logged, because whether this worked is only
# knowable from the log of the next boot.
#
# OpenRC's init reads /etc/inittab, so one respawn line is all it takes. The
# getty path is probed rather than assumed: it is not knowable from the
# initramfs which of /sbin, /bin or /usr/bin the target system actually uses.
ensure_usb_serial_getty() {
	local inittab=/sysroot/etc/inittab
	local getty_bin="" cand line

	if [ ! -e "$inittab" ]; then
		echo "INFO: $inittab does not exist, skipping the USB serial getty"
		return 0
	fi
	if grep -q 'ttyGS0' "$inittab" 2>/dev/null; then
		echo "INFO: $inittab already has a ttyGS0 entry, leaving it untouched"
		return 0
	fi
	for cand in /sysroot/sbin/getty /sysroot/bin/getty /sysroot/usr/bin/getty; do
		if [ -e "$cand" ]; then
			getty_bin="${cand#/sysroot}"
			break
		fi
	done
	if [ -z "$getty_bin" ]; then
		echo "INFO: no getty binary found under /sysroot, skipping the USB serial getty"
		return 0
	fi
	# -L keeps getty local rather than waiting on a carrier detect, which a
	# USB gadget serial port never asserts. The vt100 terminal type is what the
	# other getty lines in this file already use.
	line="ttyGS0::respawn:$getty_bin -L ttyGS0 vt100"
	if printf '%s\n' "$line" >> "$inittab" 2>/dev/null; then
		echo "INFO: added '$line' to $inittab"
	else
		echo "INFO: could not append to $inittab, there will be no USB serial login"
	fi
}
# <<< s5screen: helpers (end)
mount_subpartitions() {
	# skip if ran already (unmerged -extra)
	if [ -n "$PMOS_ROOT" ] && [ -n "$PMOS_BOOT" ]; then
		return
	fi
	try_parts="/dev/disk/by-partlabel/userdata /dev/disk/by-partlabel/system* /dev/mapper/system*"
	android_parts=""
	for x in $try_parts; do
		[ -e "$x" ] && android_parts="$android_parts $x"
	done

	local losetup_args="--show -Pf"
	if [ -n "$deviceinfo_rootfs_image_sector_size" ]; then
		losetup_args="$losetup_args --sector-size $deviceinfo_rootfs_image_sector_size"
	fi
	attempt_start=$(get_uptime_seconds)
	wait_seconds=10
	echo "Trying to mount subpartitions for $wait_seconds seconds..."
	# >>> s5screen: booting hook (begin)
	s5status booting
	# <<< s5screen: booting hook (end)

	# Subpartition init uses losetup, so make sure the loop module is loaded.
	modprobe loop 2>/dev/null || true

	find_root_partition
	while [ -z "$PMOS_ROOT" ]; do
		partitions="$android_parts $(grep -v "loop\|ram" < /proc/diskstats |\
			sed 's/\(\s\+[0-9]\+\)\+\s\+//;s/ .*//;s/^/\/dev\//')"
		for partition in $partitions; do
		    # Skip whole disks - only check partitions and logical device-mapper devices for subpartitions
			[ -e "/sys/class/block/$(basename "$partition")/partition" ] || [ -d "/sys/class/block/$(basename "$partition")/dm" ] || continue
			# Count subpartitions and only attempt to probe deeper if there are an expected
			# number of them present on the partition. This prevents us from calling losetup
			# (and then cleanup) on every single partition on the device
			#
			# Subpartitions, if there are any, are counted with fdisk because there doesn't
			# seem to be a better way to do this without adding more dependencies to the 1st
			# stage initramfs. fdisk's output differs if it's reading a GPT or MBR partition
			# table, so this regex needs to account for both, e.g.:
			#  GPT:
			#   1     2048   499711  243M primary
			#  MBR:
			# /dev/mmcblk0p62p1 *  4,4,1   979,210,2   2048  499711   97664  243M 83 Linux
			local part_count
			part_count="$(fdisk -l "$partition" 2>/dev/null | grep -cE '^ +[0-9]|^'"$partition")"
			# It's probably the right "disk" if it has 2 partitions on it.
			if [ "$part_count" -eq 2 ]; then
				echo "Mount subpartitions of $partition"
				SUBPARTITION_DEV="$partition"
				# >>> dm-bypass: mount_subpartitions call (begin)
				# This kernel cannot read the nested partition table through a
				# loop device, so try plain device-mapper linear mappings over
				# the same extents first. They mirror the kpartx tables that
				# read these filesystems correctly under recovery, and losetup
				# stays the fallback whenever a guard does not match.
				if dm_attach_subpartitions "$partition"; then
					SUBPARTITION_LOOP=""
					# >>> s5screen: dmok hook (begin)
					s5status dmok
					# <<< s5screen: dmok hook (end)
					break
				fi
				# >>> s5screen: dmno hook (begin)
				s5status dmno
				# <<< s5screen: dmno hook (end)
				# <<< dm-bypass: mount_subpartitions call (end)
				# shellcheck disable=SC2086
				SUBPARTITION_LOOP="$(losetup $losetup_args "$partition")"
				if [ -z "$SUBPARTITION_LOOP" ]; then
					echo "WARNING: failed to create loop device for $partition"
					SUBPARTITION_DEV=""
					continue
				fi
				# Ensure that this was the *correct* subpartition
				# Some devices have mmc partitions that appear to have
				# subpartitions, but aren't our subpartition.
				find_root_partition
				if [ -n "$PMOS_ROOT" ]; then
					break
				fi
				[ -n "$SUBPARTITION_LOOP" ] && losetup -d "$SUBPARTITION_LOOP"
				SUBPARTITION_DEV=""
				SUBPARTITION_LOOP=""
			fi
		done
		if [ "$(get_uptime_seconds)" -ge $(( attempt_start + wait_seconds )) ]; then
			echo "ERROR: failed to mount subpartitions!"
			# >>> s5screen: loopfail hook (begin)
			s5status loopfail
			# <<< s5screen: loopfail hook (end)
			return;
		fi
		sleep 0.1;
		# Check if partition appeared without needing subpartitions
		find_root_partition
	done
}

# Rewrite /dev/dm-X paths to /dev/mapper/...
pretty_dm_path() {
	dm="$1"
	n="${dm#/dev/dm-}"

	# If the substitution didn't do anything, then we're done
	[ "$n" = "$dm" ] && echo "$dm" && return

	# Get the name of the device mapper device
	name="/dev/mapper/$(cat "/sys/class/block/dm-${n}/dm/name")"
	echo "$name"
}

# Prints the path to the partition if found, or nothing.
find_partition() {
	# $1: UUID of partition if known
	# $2: path to partition
	# $3: label of partition
	# $4: additional blkid token to check (e.g TYPE=crypto_LUKS)

	local uuid="$1"
	local path="$2"
	local label="$3"
	local extra="$4"
	local partition

	if [ -n "$uuid" ]; then
		partition="$(blkid --uuid "$uuid")"
		if [ -z "$partition" ]; then
			# Don't fall back to anything if the given UUID wasn't
			# found, it might show up later but if not we should
			# error out.
			return
		fi
	fi

	if [ -z "$partition" ]; then
		if [ -e "$path" ]; then
			partition="$path"
		elif [ -n "$path" ]; then
			# Don't fall back to anything if the given path wasn't
			# found, it might show up later but if not we should
			# error out.
			return
		fi
	fi

	if [ -z "$partition" ]; then
		partition="$(blkid --label "$label")"
	fi

	if [ -z "$partition" ] && [ -n "$extra" ]; then
		# check for arbitrary blkid tag
		partition="$(blkid --match-token "$extra" | cut -d ":" -f 1 | head -n 1)"
	fi

	# prettify e.g. /dev/dm-0 to /dev/mapper/userdata1
	pretty_dm_path "$partition"
}

find_root_partition() {
	# $1: variable to set result to
	local result
	result=$1

	# The partition layout is one of the following:
	# a) boot, root partitions on sdcard
	# b) boot, root partition on the "system" partition (which has its
	#    own partition header! so we have partitions on partitions!)
	#
	# mount_subpartitions() must get executed before calling
	# find_root_partition(), so partitions from b) also get found.
	if [ -z "$PMOS_ROOT" ]; then
		PMOS_ROOT="$(find_partition "$root_uuid" "$root_path" "pmOS_root" "TYPE=crypto_LUKS")"
	fi

	# Set the result, since using a subshell prevents us from caching
	if [ -n "$result" ]; then eval "$result=\"$PMOS_ROOT\""; fi
}

find_boot_partition() {
	# $1: variable to set result to
	local result
	result=$1

	if [ -z "$PMOS_BOOT" ]; then
		# Before doing anything else check if we are using a stowaway
		if [ "$stowaway" = "y" ]; then
			mount_root_partition
			PMOS_BOOT="/sysroot/boot"
			mount --bind /sysroot/boot /boot
		else
			PMOS_BOOT="$(find_partition "$boot_uuid" "$boot_path" "pmOS_boot")"
		fi
	fi

	# Set the result, since using a subshell prevents us from caching
	if [ -n "$result" ]; then eval "$result=\"$PMOS_BOOT\""; fi
}

get_partition_type() {
	local partition
	partition="$1"
	blkid "$partition" | sed 's/^.*\ TYPE="\([a-zA-Z0-9_]*\)".*$/\1/'
}

get_mounted_filesystem_type() {
	awk -v mountpoint="$1" '$2 == mountpoint {print $3}' /proc/mounts
}

# $1: partition
check_filesystem() {
	local partition=""
	local status=""
	local type=""

	partition="$1"
	type="$(get_partition_type "$partition")"
	# btrfs check is not included in that list on purpose. it takes too much time
	# (as in: multiple minutes) and gets even slower the more the partition is used
	case "$type" in
		ext*)
			echo "Auto-repair and check 'ext' filesystem ($partition)"
			e2fsck -p "$partition"
			if [ $? -ge 4 ]; then
				status="fail"
			fi
			;;
		f2fs)
			echo "Auto-repair and check 'f2fs' filesystem ($partition)"
			fsck.f2fs -p "$partition"
			status=$?
			if [ $? -gt 4 ]; then
				status="fail"
			fi
			;;
		vfat)
			echo "Auto-repair and check 'vfat' filesystem ($partition)"
			fsck.vfat -p "$partition"
			if [ $? -gt 4 ]; then
				status="fail"
			fi
			;;
		xfs)
			echo "Auto-repair and check 'xfs' filesystem ($partition)"
			fsck.xfs -p "$partition"
			if [ $? -gt 4 ]; then
				status="fail"
			fi
			;;
		*)	echo "WARNING: fsck not supported for '$type' filesystem ($partition)." ;;
	esac

	if [ "$status" = "fail" ]; then
		splash_set_warning "Filesystem needs manual repair (fsck) ($partition)\nhttps://postmarketos.org/troubleshooting\n\nBoot anyways by pressing Volume-Up or Left-Shift..."
		while ! iskey KEY_LEFTSHIFT KEY_VOLUMEUP ; do
			:
		done
	fi

	splash_set_message "Loading"
}

# $1: path
# $2: set to "rw" for read-write
# Mount the boot partition. It gets mounted twice, first at /boot (ro), then at
# /sysroot/boot (rw), after root has been mounted at /sysroot, so we can
# switch_root to /sysroot and have the boot partition properly mounted.
mount_boot_partition() {
	local partition
	local mount_opts

	find_boot_partition partition
	mount_opts="-o nodev,nosuid,noexec"

	# We dont need to do this when using stowaways
	if [ "$stowaway" = "y" ]; then
		return
	fi

	if [ "$2" = "rw" ]; then
		check_filesystem "$partition"
		echo "Mount boot partition ($partition) to $1 (read-write)"
	else
		mount_opts="$mount_opts,ro"
		echo "Mount boot partition ($partition) to $1 (read-only)"
	fi

	type="$(get_partition_type "$partition")"
	case "$type" in
		ext*)
			modprobe ext4
			# ext2 might be handled by the ext2 or ext4 kernel module
			# so let mount detect that automatically by omitting -t
			;;
		xfs)
			modprobe xfs
			;;
		vfat)
			modprobe vfat
			mount_opts="-t vfat $mount_opts,umask=0077,nosymfollow,codepage=437,iocharset=ascii"
			;;
		*)	echo "WARNING: Detected unsupported '$type' filesystem ($partition)." ;;
	esac

	# shellcheck disable=SC2086
	mount $mount_opts "$partition" "$1"
}

# $1: initramfs-extra path
extract_initramfs_extra() {
	local initramfs_extra
	initramfs_extra="$1"
	if [ ! -e "$initramfs_extra" ]; then
		echo "ERROR: initramfs-extra not found!"
		splash_set_error "initramfs-extra not found\nhttps://postmarketos.org/troubleshooting"
		fail_halt_boot
	fi
	echo "Extract $initramfs_extra"
	# uncompressed:
	# cpio -di < "$initramfs_extra"
	gzip -d -c "$initramfs_extra" | cpio -iu
}

wait_partition() {
	local description findfunc partition
	description="$1"
	findfunc="$2"

	$findfunc partition
	if [ -n "$partition" ]; then
		return
	fi

	splash_set_message "Waiting for $description partition"
	for _ in $(seq 1 30); do
		sleep 1
		$findfunc partition
		if [ -n "$partition" ]; then
			return
		fi
		check_keys ""
	done

	splash_set_error "$description partition not found!\nhttps://postmarketos.org/troubleshooting"
	fail_halt_boot
}

wait_boot_partition() {
	wait_partition "boot" "find_boot_partition"
}

wait_root_partition() {
	wait_partition "root" "find_root_partition"
}

delete_old_install_partition() {
	local partition
	# The on-device installer leaves a "pmOS_deleteme" (p3) partition after
	# successful installation, located after "pmOS_root" (p2). Delete it,
	# so we can use the space.

	# We definitely found the partition by this point so no need to call
	# find_root_partition
	partition="$(echo "$PMOS_ROOT" | sed 's/2$/3/')"
	if ! blkid "$partition" | grep -q pmOS_deleteme; then
		return
	fi

	device="$(echo "$partition" | sed -E 's/p?3$//')"
	echo "First boot after running on-device installer - deleting old" \
		"install partition: $partition"
	parted -s "$device" rm 3
}

# $1: path to device
has_unallocated_space() {
	# Check if there is unallocated space at the end of the device
	parted -s "$1" print free | tail -n2 | \
		head -n1 | grep -qi "free space"
}

mount_root_partition() {
	# Don't mount root if it is already mounted
	if mountpoint -q /sysroot; then
		return
	fi

	local partition

	find_root_partition partition

	echo "Mount root partition ($partition) to /sysroot (read-write) with options ${rootfsopts#,}"
	type="$(get_partition_type "$partition")"
	info "Detected $type filesystem"

	case "$type" in
		btrfs|ext4|f2fs|xfs)
			;;
		*)
			echo "ERROR: Detected unsupported '$type' filesystem ($partition)."
			splash_set_error "Unsupported '$type' filesystem ($partition)\nhttps://postmarketos.org/troubleshooting"
			fail_halt_boot
			;;
	esac

	if ! modprobe "$type"; then
		info "Unable to load module '$type' - assuming it's built-in"
	fi

	# btrfs may be using multiple backing block devices, scan for the rest of them
	if [ "$type" = "btrfs" ]; then
		btrfs device scan
	fi

	if ! mount -t "$type" -o rw"$rootfsopts" "$partition" /sysroot; then
		echo "ERROR: unable to mount root partition!"
		splash_set_error "Unable to mount root partition\nhttps://postmarketos.org/troubleshooting"
		fail_halt_boot
	fi

	if [ -e /sysroot/.stowaways/pmos/etc/os-release ]; then
		umount /sysroot

		mkdir /stowaway
		mount -t "$type" -o rw"$rootfsopts" "$partition" /stowaway
		mount --bind /stowaway/.stowaways/pmos/ /sysroot
	fi

	if ! [ -e /sysroot/etc/os-release ]; then
		splash_set_error "Root partition does not contain a root filesystem\nhttps://postmarketos.org/troubleshooting"
		fail_halt_boot
	fi
	# >>> s5screen: booted hook (begin)

	s5status booted
	# Root is mounted read-write here, so this is the first point at which
	# the installed system can be given a USB serial login prompt.
	ensure_usb_serial_getty
	# <<< s5screen: booted hook (end)
}

# $1: path to the hooks dir
run_hooks() {
	local scriptsdir
	local hook
	scriptsdir="$1"

	if [ -z "$(ls -A "$scriptsdir" 2>/dev/null)" ]; then
		return
	fi

	for hook in "$scriptsdir"/*.sh; do
		info "Running initramfs hook: $hook"
		sh "$hook"
	done
}

setup_usb_network_android() {
	local sysfs_dir android_function
	# Only run, when we have the android usb driver
	sysfs_dir=/sys/class/android_usb/android0
	if ! [ -e "$sysfs_dir" ]; then
		return
	fi

	info "  Setting up USB gadget through android_usb"

	usb_idVendor="$(echo "${deviceinfo_usb_idVendor:-0x18D1}" | sed "s/0x//g")"	# default: Google Inc.
	usb_idProduct="$(echo "${deviceinfo_usb_idProduct:-0xD001}" | sed "s/0x//g")"	# default: Nexus 4 (fastboot)
	android_function="${deviceinfo_usb_network_function:-rndis.usb0}"

	# Do the setup
	echo "0" >"$sysfs_dir/enable"
	echo "$usb_idVendor" >"$sysfs_dir/idVendor"
	echo "$usb_idProduct" >"$sysfs_dir/idProduct"
	echo "${android_function%%.*}" >"$sysfs_dir/functions"
	echo "1" >"$sysfs_dir/enable"
}

get_usb_udc() {
	local _udc_dev="${deviceinfo_usb_network_udc:-}"
	if [ -z "$_udc_dev" ]; then
		# shellcheck disable=SC2012
		_udc_dev=$(ls /sys/class/udc | head -1)
	fi

	echo "$_udc_dev"
}

setup_usb_configfs_udc() {
	# Check if there's an USB Device Controller
	local _udc_dev
	_udc_dev="$(get_usb_udc)"

	# Remove any existing UDC to avoid "write error: Resource busy" when setting UDC again
	if [ "$(wc -w <$CONFIGFS/g1/UDC)" -gt 0 ]; then
		echo "" > "$CONFIGFS"/g1/UDC || echo "  Couldn't write to clear UDC"
	fi
	# Link the gadget instance to an USB Device Controller. This activates the gadget.
	# See also: https://gitlab.postmarketos.org/postmarketOS/pmbootstrap/issues/338
	echo "$_udc_dev" > "$CONFIGFS"/g1/UDC || echo "  Couldn't write new UDC"
}

# $1: if set, skip writing to the UDC
setup_usb_network_configfs() {
	# See: https://www.kernel.org/doc/Documentation/usb/gadget_configfs.txt
	local skip_udc="$1"

	if ! [ -e "$CONFIGFS" ]; then
		info "usb_gadget not found in configfs, skipping gadget setup..."
		return
	fi

	if [ -z "$(get_usb_udc)" ]; then
		info "No UDC found, skipping gadget setup..."
		return
	fi

	# Default values for USB-related deviceinfo variables
	usb_idVendor="${deviceinfo_usb_idVendor:-0x18D1}"   # default: Google Inc.
	usb_idProduct="${deviceinfo_usb_idProduct:-0xD001}" # default: Nexus 4 (fastboot)
	usb_serialnumber="${deviceinfo_usb_serialnumber:-postmarketOS}"
	usb_network_function="${deviceinfo_usb_network_function:-ncm.usb0}"
	usb_network_function_fallback="rndis.usb0"
	usb_network_host_addr="${deviceinfo_usb_network_host_addr:-}"

	echo "  Setting up USB gadget through configfs"
	# Create an usb gadet configuration
	mkdir $CONFIGFS/g1 || echo "  Couldn't create $CONFIGFS/g1"
	echo "$usb_idVendor"  > "$CONFIGFS/g1/idVendor"
	echo "$usb_idProduct" > "$CONFIGFS/g1/idProduct"

	# Create english (0x409) strings
	mkdir $CONFIGFS/g1/strings/0x409 || echo "  Couldn't create $CONFIGFS/g1/strings/0x409"

	# shellcheck disable=SC2154
	echo "$deviceinfo_manufacturer" > "$CONFIGFS/g1/strings/0x409/manufacturer"
	echo "$usb_serialnumber"        > "$CONFIGFS/g1/strings/0x409/serialnumber"
	# shellcheck disable=SC2154
	echo "$deviceinfo_name"         > "$CONFIGFS/g1/strings/0x409/product"

	# Create network function.
	if ! mkdir $CONFIGFS/g1/functions/"$usb_network_function"; then
		# Try the fallback function next
		if mkdir $CONFIGFS/g1/functions/"$usb_network_function_fallback"; then
			usb_network_function="$usb_network_function_fallback"
		fi
	fi

	# Create configuration instance for the gadget
	mkdir $CONFIGFS/g1/configs/c.1 \
		|| echo "  Couldn't create $CONFIGFS/g1/configs/c.1"
	mkdir $CONFIGFS/g1/configs/c.1/strings/0x409 \
		|| echo "  Couldn't create $CONFIGFS/g1/configs/c.1/strings/0x409"
	echo "USB network" > $CONFIGFS/g1/configs/c.1/strings/0x409/configuration \
		|| echo "  Couldn't write configration name"
	if [ -n "$usb_network_host_addr" ]; then
		echo "$usb_network_host_addr" > $CONFIGFS/g1/functions/"$usb_network_function"/host_addr \
			|| echo "  Couldn't write host addr"
	fi


	# Link the network instance to the configuration
	ln -s $CONFIGFS/g1/functions/"$usb_network_function" $CONFIGFS/g1/configs/c.1 \
		|| echo "  Couldn't symlink $usb_network_function"

	# If an argument was supplied then skip writing to the UDC (only used for mass storage
	# log recovery)
	if [ -z "$skip_udc" ]; then
		setup_usb_configfs_udc
	fi
}

setup_usb_network() {
	# Only run once
	_marker="/tmp/_setup_usb_network"
	[ -e "$_marker" ] && return
	touch "$_marker"
	info "Setup usb network"
	# Run all usb network setup functions (add more below!)
	setup_usb_network_android
	setup_usb_network_configfs
}

start_unudhcpd() {
	# Only run once
	[ "$(pidof unudhcpd)" ] && return

	local usb_iface
	# Don't run if there's no USB gadget.
	if [ -z "$(cat "$CONFIGFS/g1/UDC" 2>/dev/null)" ] &&
		! [ -e /sys/class/android_usb/android0 ]; then
		return
	fi

	# Skip if disabled
	# shellcheck disable=SC2154
	if [ "$deviceinfo_disable_dhcpd" = "true" ]; then
		return
	fi

	local client_ip="${unudhcpd_client_ip:-172.16.42.2}"
	info "Starting unudhcpd with server ip $HOST_IP, client ip: $client_ip"

	# Get usb interface
	usb_network_function="${deviceinfo_usb_network_function:-ncm.usb0}"
	usb_network_function_fallback="rndis.usb0"
	if [ -n "$(cat $CONFIGFS/g1/UDC)" ]; then
		usb_iface="$(
			cat "$CONFIGFS/g1/functions/$usb_network_function/ifname" 2>/dev/null ||
			cat "$CONFIGFS/g1/functions/$usb_network_function_fallback/ifname" 2>/dev/null ||
			echo ''
		)"
	else
		usb_iface=""
	fi
	if [ -n "$usb_iface" ]; then
		ifconfig "$usb_iface" "$HOST_IP"
	elif ifconfig ncm0 "$HOST_IP" 2>/dev/null; then
		usb_iface=ncm0
	elif ifconfig rndis0 "$HOST_IP" 2>/dev/null; then
		usb_iface=rndis0
	elif ifconfig usb0 "$HOST_IP" 2>/dev/null; then
		usb_iface=usb0
	elif ifconfig eth0 "$HOST_IP" 2>/dev/null; then
		usb_iface=eth0
	fi

	if [ -z "$usb_iface" ]; then
		echo "  Could not find an interface to run a dhcp server on"
		echo "  Interfaces:"
		ip link
		return
	fi

	info "  Using interface $usb_iface"
	info "  Starting the DHCP daemon"
	(
		unudhcpd -i "$usb_iface" -s "$HOST_IP" -c "$client_ip"
	) &
}

setup_usb_acm_configfs() {
	local active_udc
	active_udc="$(cat $CONFIGFS/g1/UDC)"

	if ! [ -e "$CONFIGFS" ]; then
		echo "  $CONFIGFS does not exist, can't set up serial gadget"
		return 1
	fi

	# unset UDC
	echo "" > $CONFIGFS/g1/UDC

	# Create acm function
	mkdir "$CONFIGFS/g1/functions/$CONFIGFS_ACM_FUNCTION" \
		|| echo "  Couldn't create $CONFIGFS/g1/functions/$CONFIGFS_ACM_FUNCTION"

	# Link the acm function to the configuration
	ln -s "$CONFIGFS/g1/functions/$CONFIGFS_ACM_FUNCTION" "$CONFIGFS/g1/configs/c.1" \
		|| echo "  Couldn't symlink $CONFIGFS_ACM_FUNCTION"

	return 0
}

# Spawn a subshell to restart the getty if it exits
# $1: tty
run_getty() {
	{
		# Due to how the Linux host ACM driver works, we need to wait
		# for data to be sent from the host before spawning the getty.
		# Otherwise our README message will be echo'd back all garbled.
		# On Linux in particular, there is a hack we can use: by writing
		# something to the port, it will be echo'd back at the moment the
		# port on the host side is opened, so user input won't even be
		# needed in most cases. For more info see the blog posts at:
		# https://michael.stapelberg.ch/posts/2021-04-27-linux-usb-virtual-serial-cdc-acm/
		# https://connolly.tech/posts/2024_04_15-broken-connections/
		if [ "$1" = "ttyGS0" ]; then
			echo " " > /dev/ttyGS0
			# shellcheck disable=SC3061
			read -r < /dev/ttyGS0
		fi
		while /sbin/getty -n -l /sbin/pmos_getty "$1" 115200 vt100; do
			sleep 0.2
		done
	} &
}

setup_usb_storage_configfs() {
	local active_udc
	local storage_dev
	active_udc="$(cat $CONFIGFS/g1/UDC)"
	storage_dev="$1"

	if ! [ -e "$CONFIGFS" ]; then
		echo "  $CONFIGFS does not exist, can't set up storage gadget"
		return 1
	fi

	if [ -z "$storage_dev" ]; then
		if [ -e "$CONFIGFS/g1/configs/c.1/$CONFIGFS_MASS_STORAGE_FUNCTION" ]; then
			echo "Disabling USB mass storage gadget"
			unlink "$CONFIGFS/g1/configs/c.1/$CONFIGFS_MASS_STORAGE_FUNCTION"
			setup_usb_configfs_udc
		fi
		return 0
	fi

	if ! [ -b "$storage_dev" ]; then
		echo "  Storage device '$storage_dev' is not a block device"
		return 1
	fi

	# Set up network gadget if not already done
	if [ -z "$active_udc" ]; then
		setup_usb_network_configfs "skip_udc"
	else
		# Unset UDC before reconfiguring gadget
		echo "" > $CONFIGFS/g1/UDC
	fi

	# Create mass storage function
	mkdir -p "$CONFIGFS/g1/functions/$CONFIGFS_MASS_STORAGE_FUNCTION" \
		|| echo "  Couldn't create $CONFIGFS/g1/functions/$CONFIGFS_MASS_STORAGE_FUNCTION"

	echo "$storage_dev" > "$CONFIGFS/g1/functions/$CONFIGFS_MASS_STORAGE_FUNCTION/lun.0/file"

	# Link the mass storage function to the configuration
	ln -sf "$CONFIGFS/g1/functions/$CONFIGFS_MASS_STORAGE_FUNCTION" "$CONFIGFS/g1/configs/c.1" \
		|| echo "  Couldn't symlink $CONFIGFS_MASS_STORAGE_FUNCTION"

	setup_usb_configfs_udc
	return 0
}

debug_shell() {
	splash_hide
	echo "Entering debug shell"
	# if we have a UDC it's already been configured for USB networking
	local have_udc
	have_udc="$(cat $CONFIGFS/g1/UDC)"

	# Use USB ACM gadget if no UDC is available,
	# if UDC is available use configured USB networking,
	# but DHCP server needs to be started
	if [ -n "$have_udc" ]; then
		setup_usb_acm_configfs
	else
		start_unudhcpd
	fi

	# mount pstore, if possible
	if [ -d /sys/fs/pstore ]; then
		mount -t pstore pstore /sys/fs/pstore || true
	fi

	mount -t debugfs none /sys/kernel/debug || true
	# make a symlink like Android recoveries do
	ln -s /sys/kernel/debug /d

	cat <<-EOF > /README
	postmarketOS debug shell
	https://postmarketos.org/debug-shell

	  Device: $deviceinfo_name ($deviceinfo_codename)
	  Kernel: $(uname -r)
	  OS ver: $VERSION
	  initrd: $INITRAMFS_PKG_VERSION

	Run 'pmos_continue_boot' to continue booting.
	Read the initramfs log with 'cat /pmOS_init.log'.
	EOF

	# Add pmos_logdump message only if relevant
	if [ -n "$have_udc" ]; then
		echo "Run 'pmos_logdump' to generate a log dump and expose it over USB." >> /README

		cat <<-EOF >> /README
		You can expose storage devices over USB with
		'setup_usb_storage_configfs /dev/DEVICE'
		EOF

		if [ -n "$usb_storage" ]; then
			echo "$usb_storage is exposed over USB by default (pmos.usb-storage)" >> /README
		fi
	fi

	# Display some info
	cat <<-EOF > /etc/profile
	cat /README
	. /init_functions.sh
	EOF

	cat <<-EOF > /sbin/pmos_getty
	#!/bin/sh
	/bin/sh -l
	EOF
	chmod +x /sbin/pmos_getty

	cat <<-EOF > /sbin/pmos_continue_boot
	#!/bin/sh
	echo "Continuing boot..."
	touch /tmp/continue_boot
	pkill -f telnetd.*:23
	while sleep 1; do :; done
	EOF
	chmod +x /sbin/pmos_continue_boot

	cat <<-EOF > /sbin/pmos_logdump
	#!/bin/sh
	echo "Dumping logs, check for a new mass storage device"
	touch /tmp/dump_logs
	EOF
	chmod +x /sbin/pmos_logdump

	# Get the console (ttyX) associated with /dev/console
	local active_console
	active_console="$(cat /sys/devices/virtual/tty/tty0/active)"
	# Get a list of all active TTYs include serial ports
	local serial_ports
	serial_ports="$(cat /sys/devices/virtual/tty/console/active)"
	# Get the getty device too (might not be active)
	local getty
	getty="$(echo "$deviceinfo_getty" | cut -d';' -f1)"

	# Run getty's on the consoles
	for tty in $serial_ports; do
		# Some ports we handle explicitly below to make sure we don't
		# accidentally spawn two getty's on them
		if echo "tty0 tty1 ttyGS0 $getty" | grep -q "$tty" ; then
			continue
		fi
		run_getty "$tty"
	done

	if [ -n "$getty" ]; then
		run_getty "$getty"
	fi

	# Rewrite tty to tty1 if tty0 is active
	if [ "$active_console" = "tty0" ]; then
		active_console="tty1"
	fi

	# Getty on the display
	splash_hide
	# Spawn buffyboard if the device might not have a physical keyboard
	# buffyboard is only available with merged initramfs-extra!
	if command -v buffyboard 2>/dev/null && \
	   echo "handset tablet convertible" | grep "${deviceinfo_chassis:-handset}" >/dev/null; then
		modprobe uinput
		# Set a large font for the framebuffer
		setfont "/usr/share/consolefonts/ter-128n.psf.gz" -C "/dev/$active_console"
		buffyboard &
	fi
	run_getty "$active_console"

	# And on the usb acm port (if it exists)
	if [ -e /dev/ttyGS0 ]; then
		run_getty ttyGS0
	fi

	# To avoid racing with the host PC opening the ACM port, we spawn
	# the getty first. See the comment in run_getty for more details.
	setup_usb_configfs_udc

	# Spawn telnetd for those who prefer it. ACM gadget mode is not
	# supported on some old kernels so this exists as a fallback.
	telnetd -b "${HOST_IP}:23" -l /sbin/pmos_getty &

	# Set up USB mass storage if pmos.usb-storage= was specified on cmdline
	[ -z "$usb_storage" ] || setup_usb_storage_configfs "$usb_storage"

	# wait until we get the signal to continue boot
	while ! [ -e /tmp/continue_boot ]; do
		sleep 0.2
		if [ -e /tmp/dump_logs ]; then
			rm -f /tmp/dump_logs
			export_logs
		fi
	done

	# Remove the ACM/mass storage gadget devices
	# FIXME: would be nice to have a way to keep this on and
	# pipe kernel/init logs to it.
	rm -f $CONFIGFS/g1/configs/c.1/"$CONFIGFS_ACM_FUNCTION"
	rmdir $CONFIGFS/g1/functions/"$CONFIGFS_ACM_FUNCTION"
	rm -f "$CONFIGFS/g1/configs/c.1/$CONFIGFS_MASS_STORAGE_FUNCTION"
	rmdir "$CONFIGFS/g1/functions/$CONFIGFS_MASS_STORAGE_FUNCTION"
	setup_usb_configfs_udc

	splash_set_message "Loading"

	pkill -f buffyboard || true
}

# Check if the user is pressing a key and either drop to a shell or halt boot as applicable
check_keys() {
	{
		# If the user is pressing either the left control key or the volume down
		# key then drop to a debug shell.
		if iskey KEY_LEFTCTRL KEY_VOLUMEDOWN; then
			beebzzr -b 2 -d 100 &
			debug_shell
		# If instead they're pressing left shift or volume up, then fail boot
		# and dump logs
		elif iskey KEY_LEFTSHIFT KEY_VOLUMEUP; then
			# >>> s5screen: check_keys guard (begin)
			# iskey is not trusted on a single sample: see keys_still_held().
			if keys_still_held KEY_LEFTSHIFT KEY_VOLUMEUP; then s5status keys
			# <<< s5screen: check_keys guard (end)
			fail_halt_boot
			# >>> s5screen: check_keys guard tail (begin)
			fi
			# <<< s5screen: check_keys guard tail (end)
		fi

		touch /tmp/debug_shell_exited
	} &

	while ! [ -e /tmp/debug_shell_exited ]; do
		sleep 1
	done
}

# Show the Plymouth splash screen
# Uses: nosplash
# Sets: (none)
# Returns: 0
splash_show() {
	if [ "$nosplash" = "y" ]; then
		return
	fi

	if plymouth --ping 2>/dev/null; then
		plymouth show-splash
	fi
}

# Hide the Plymouth splash screen
# Uses: nosplash
# Sets: (none)
# Returns: 0
splash_hide() {
	if [ "$nosplash" = "y" ]; then
		return
	fi

	if plymouth --ping 2>/dev/null; then
		plymouth hide-splash
	fi
}

# Set the Plymouth message
# Uses: (none)
# Sets: (none)
# $1: message text to display, may be multiline with \n
# Returns: 0
splash_set_message() {
	info "SPLASH: $1"
	splash_show
	if plymouth --ping 2>/dev/null; then
		# Use printf to convert \n to literal newlines for multiline messages
		plymouth display-message --text "$(printf '%b' "$1")"
	fi
}

# Set an error on the Plymouth splash
# Uses: (none)
# Sets: (none)
# $1: error text to display
# Returns: 0
splash_set_error() {
	info "SPLASH ERROR: $1"
	splash_show
	if plymouth --ping 2>/dev/null; then
		plymouth update --status="error"
		# Use printf to convert \n to literal newlines for multiline messages
		plymouth display-message --text "$(printf '%b' "$1")"
	fi
}

# Set a warning on the Plymouth splash
# Uses: (none)
# Sets: (none)
# $1: warning text to display
# Returns: 0
splash_set_warning() {
	info "SPLASH WARNING: $1"
	splash_show
	if plymouth --ping 2>/dev/null; then
		plymouth update --status="warning"
		# Use printf to convert \n to literal newlines for multiline messages
		plymouth display-message --text "$(printf '%b' "$1")"
	fi
}

set_framebuffer_mode() {
	[ -e "/sys/class/graphics/fb0/modes" ] || return
	[ -z "$(cat /sys/class/graphics/fb0/mode)" ] || return

	_mode="$(cat /sys/class/graphics/fb0/modes)"
	echo "Setting framebuffer mode to: $_mode"
	echo "$_mode" > /sys/class/graphics/fb0/mode
}

setup_framebuffer() {
	if [ "$nosplash" = "y" ]; then
		return
	fi

	# Wait for /dev/fb0
	for _ in $(seq 1 100); do
		[ -e "/dev/fb0" ] && break
		sleep 0.1
	done
	if ! [ -e "/dev/fb0" ]; then
		echo "ERROR: /dev/fb0 did not appear after waiting 10 seconds!"
		echo "If your device does not have a framebuffer, disable this with:"
		echo "no_framebuffer=true in <https://postmarketos.org/deviceinfo>"
		return
	fi

	set_framebuffer_mode
}

setup_bootchart2() {
	if [ "$bootchart2" = "y" ]; then
		if [ -f "/sysroot/sbin/bootchartd" ]; then
			# shellcheck disable=SC2034
			init="/sbin/bootchartd"
			echo "remounting /sysroot as rw for /sbin/bootchartd"
			mount -o remount, rw /sysroot

			# /dev/null may not exist at the first boot after
			# the root filesystem has been created.
			[ -c /sysroot/dev/null ] && return
			echo "creating /sysroot/dev/null for /sbin/bootchartd"
			mknod -m 666 "/sysroot/dev/null" c 1 3
		else
			echo "WARNING: bootchart2 is not installed."
		fi
	fi
}

mkhash() {
	sha256sum "$1" | cut -d " " -f 1
}

# Create a small disk image and copy logs to it so they can be exposed via mass storage
create_logs_disk() {
	local loop_dev="$1"
	local upload_file=""
	echo "Creating logs disk"

	dd if=/dev/zero of=/tmp/logs.img bs=1M count=32
	# The log device used is assumed to be $loop_dev
	losetup -f /tmp/logs.img
	mkfs.vfat -n "PMOS_LOGS" "$loop_dev"
	mkdir -p /tmp/logs
	modprobe vfat
	mount "$loop_dev" /tmp/logs

	# Copy logs
	cp /pmOS_init.log /tmp/logs/pmOS_init.txt
	dmesg > /tmp/logs/dmesg.txt
	blkid > /tmp/logs/blkid.txt
	cat /proc/cmdline > /tmp/logs/cmdline.txt
	cat /proc/partitions > /tmp/logs/partitions.txt
	# Include FDT if it exists
	[ -e /sys/firmware/fdt ] && cp /sys/firmware/fdt /tmp/logs/fdt.dtb

	# Additional info about the initramfs
	{
		echo "initramfs-version: $INITRAMFS_PKG_VERSION"
		# Take hashes of the initramfs files so we can be sure they weren't modified inadvertantly
		echo "init-hash: $(mkhash /init)"
		echo "init-functions-hash: $(mkhash /init_functions.sh)"
	} >> /tmp/logs/_info

	# Create a tar file with all the logs. We don't include the date because on many devices
	# (especially Qualcomm) the RTC is likely wrong.
	upload_file="${deviceinfo_codename}-${VERSION}-$(uname -r).tar.gz"
	# Done in a subshell to not change the working directory of init
	(cd /tmp/logs || ( echo "Couldn't cd to /tmp/logs"; return ); tar -cv ./* | gzip -6 -c > "/tmp/$upload_file")
	mv "/tmp/$upload_file" /tmp/logs/

	# Create a README with instructions on how to report an issue
	cat > /tmp/logs/README.txt <<-EOF
	Something went wrong and your device did not boot properly. If this was unexpected
	then please open a new issue by visiting

	https://gitlab.postmarketos.org/postmarketOS/pmaports/-/issues/new

	and attach the following file by dragging it onto the page:

	* $upload_file

	You are running postmarketOS $VERSION on kernel $(uname -r).
	EOF

	# Unmount
	umount /tmp/logs
}

# Make logs available via mass storage gadget
export_logs() {
	local loop_dev
	loop_dev="$(losetup -f)"
	create_logs_disk "$loop_dev"

	echo "Making logs available via mass storage"
	setup_usb_storage_configfs "$loop_dev"
}

fail_halt_boot() {
	beebzzr -b 3 -d 250 &
	export_logs
	debug_shell
	echo "Looping forever"
	while true; do
		sleep 1
	done
}
