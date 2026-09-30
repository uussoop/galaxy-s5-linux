#!/bin/sh
# Dry run of the r17 USB serial gadget logic against a mock configfs.
#
# r17 rests on a claim about kernel behaviour that is worth more than an
# argument: a function can be created and linked into configs/c.1 while the
# controller is unbound, and the kernel refuses to add one to an already-active
# configuration. The mock enforces that rule, so a regression that reorders the
# region back to where r16 had it fails here rather than on the phone.
#
# The whole shipped setup_usb_network_configfs is extracted from the real file
# and run, with only mkdir, ln and the UDC write stubbed. The ordering under
# test is therefore the shipped text, not a copy of it.
#
#   $1  the r17 init_functions.sh
#
# Nothing outside a throwaway temp tree is touched and /dev is never written.

# No `set -u` here on purpose: the shipped function reads `local skip_udc="$1"`
# and is called with no arguments, which is fine in init_functions.sh because
# that file does not use set -u. Adding it in the mock would fail on the shipped
# text for a reason that cannot occur on the device.

FUNCTIONS="$1"
[ -n "$FUNCTIONS" ] || { echo "usage: mock_usb_gadget_r17.sh <init_functions.sh>"; exit 2; }

MOCK="$(mktemp -d)"
KEEP=${KEEP:-0}
trap '[ "$KEEP" = 1 ] || rm -rf "$MOCK"' EXIT

CONFIGFS="$MOCK/usb_gadget"
UDC_NAME="dummy_udc"
FUNCTIONS_IN="$FUNCTIONS"

# Stands in for the /dev/ttyGS0 the acm driver creates when it loads, whether
# or not any function was linked. Set to a path that exists to reproduce a
# false positive.
FAKE_TTYGS="$MOCK/ttyGS0"
[ -n "${SIMULATE_TTYGS:-}" ] && : > "$FAKE_TTYGS"

# The controller is bound exactly when the UDC attribute is non-empty, so that
# attribute is the single source of truth here. The shipped
# setup_usb_configfs_udc is extracted from the real file and therefore wins
# over any stub, which is the point: the bind under test is the shipped one.
bound() { [ -s "$CONFIGFS/g1/UDC" ]; }

# Only the three kernel-behaviour primitives are stubbed.

# mkdir: ncm and rndis are absent from this kernel, so both fail the way the
# boot log shows. acm.usb0 is refused with EBUSY once the controller is bound,
# which is the r16 failure being guarded against.
#
# Real configfs creates a whole subtree in one mkdir; an ordinary mkdir -p
# matches that closely enough here, and the one behaviour that matters -- the
# acm refusal -- is modelled explicitly.
mkdir() {
	for a in "$@"; do
		case "$a" in
		*/functions/ncm.usb0 | */functions/rndis.usb0)
			echo "mkdir: can't create directory '$a': Function not implemented" >&2
			return 1
			;;
		*/functions/acm.usb0)
			if bound; then
				echo "mkdir: can't create directory '$a': Resource busy" >&2
				return 1
			fi
			;;
		esac
	done
	command mkdir -p "$@"
	# The UDC attribute is a file the kernel provides once the gadget exists,
	# and setup_usb_configfs_udc both reads and writes it, so it has to appear
	# as soon as g1 does.
	for a in "$@"; do
		if [ "$a" = "$CONFIGFS/g1" ]; then
			: > "$a/UDC"
		fi
	done
	return 0
}

# ln: link SRC into the directory DST. Leading flags are skipped, since the
# shipped code uses `ln -s` and the r17 region uses `ln -sf`. Linking into
# configs/c.1 needs the function directory to exist, and needs the controller
# unbound.
ln() {
	while [ $# -gt 0 ]; do
		case "$1" in
		-*) shift ;;
		*) break ;;
		esac
	done
	src="$1"
	dst_dir="$2"
	[ -n "$src" ] && [ -n "$dst_dir" ] || { echo "ln: bad arguments" >&2; return 1; }
	name="${src##*/}"
	command mkdir -p "$dst_dir"
	if [ ! -d "$src" ]; then
		echo "ln: can't create symbol '$dst_dir/$name': No such file or directory" >&2
		return 1
	fi
	if bound; then
		echo "ln: can't create symbol '$dst_dir/$name': Resource busy" >&2
		return 1
	fi
	command ln -s "$src" "$dst_dir/$name"
}

get_usb_udc() { echo "$UDC_NAME"; }

# The shipped helper, with the controller's state modelled. Both errors the r16
# boot log recorded are reproduced: ENXIO when clearing an unbound controller,
# and EBUSY when binding a second time.
setup_usb_configfs_udc() {
	if [ -f "$CONFIGFS/g1/UDC" ]; then
		if ! bound; then
			echo "ash: write error: No such device" >&2
			echo "  Couldn't clear UDC" >&2
		fi
		: > "$CONFIGFS/g1/UDC"
	fi
	if bound; then
		echo "ash: write error: Resource busy" >&2
		echo "  Couldn't write new UDC" >&2
		return 1
	fi
	echo "$UDC_NAME" > "$UDC_STATE"
	echo "$UDC_NAME" > "$CONFIGFS/g1/UDC"
}

command mkdir -p "$CONFIGFS"

info() { echo "INFO: $*"; }

setup_usb_network_android() { :; }

{
	echo "CONFIGFS='$CONFIGFS'"
	echo "FAKE_TTYGS='$FAKE_TTYGS'"
	echo "CONFIGFS_ACM_FUNCTION='acm.usb0'"
	echo "CONFIGFS_MASS_STORAGE_FUNCTION='mass_storage.0'"
	echo "deviceinfo_usb_idVendor=0x18D1"
	echo "deviceinfo_usb_idProduct=0xD001"
	echo "deviceinfo_usb_serialnumber=postmarketOS"
	echo "deviceinfo_usb_network_function=ncm.usb0"
	echo "deviceinfo_usb_network_host_addr="
	echo "deviceinfo_manufacturer=postmarketOS"
	echo "deviceinfo_name=galaxy-s5"
	awk '/^setup_usb_configfs_udc\(\) \{/,/^\}/' "$FUNCTIONS_IN"
	awk '/^setup_usb_network_configfs\(\) \{/,/^\}/' "$FUNCTIONS_IN"
	echo
	echo "setup_usb_network_configfs"
} > "$MOCK/harness.sh"

echo "### mock tree: $MOCK"
echo "### phase 1: the whole shipped setup_usb_network_configfs (r17 text)"
echo "### (sourced, so the stubs apply to the shipped text)"
echo "------------------------------------------------------------------"
. "$MOCK/harness.sh"
echo "------------------------------------------------------------------"
echo "UDC is now:  [$(cat "$CONFIGFS/g1/UDC" 2>/dev/null)]"
echo "functions/:  $(ls "$CONFIGFS/g1/functions" 2>/dev/null | tr '\n' ' ')"
echo "configs/c.1/: $(ls "$CONFIGFS/g1/configs/c.1" 2>/dev/null | tr '\n' ' ')"

# ---- phase 2: does ensure_usb_serial report the truth? --------------------
#
# r16 tested for /dev/ttyGS0, which the acm driver creates on load whether or
# not a function was ever linked, so it reported success on the very boot where
# every gadget step had failed. The verdict has to follow the link in the
# configuration instead, and this phase asserts that it does.
echo
echo "### phase 2: ensure_usb_serial's verdict on that state"
# Only half 1 is extracted: /sysroot does not exist here, so half 2 would
# report that and return, and the gadget half is what is under test.
sed -n '/# --- half 1: report what the early setup actually achieved ---/,/# --- half 2: the getty ---/p' \
	"$FUNCTIONS_IN" > "$MOCK/half1.sh"
if [ ! -s "$MOCK/half1.sh" ]; then
echo "### phase 2: SKIPPED -- this file has no r17 half-1 reporting block"
echo "### (r16 and earlier report on /dev/ttyGS0 instead, which is what phase"
echo "###  2 exists to show is the wrong test; their phase 1 result stands)"
LINKED=no
[ -e "$CONFIGFS/g1/configs/c.1/acm.usb0" ] && LINKED=yes
BOUND=yes
[ -s "$CONFIGFS/g1/UDC" ] && BOUND=yes
if [ "$LINKED" = yes ] && [ "$BOUND" = yes ]; then
	echo "RESULT: acm.usb0 IS linked into c.1 and the controller IS bound"
	exit 0
fi
echo "RESULT: acm.usb0 is NOT linked into c.1"
exit 1
fi
VERDICT_OUT="$MOCK/verdict.txt"
echo "FAKE_TTYGS='$FAKE_TTYGS'" >> "$MOCK/half1.sh"
. "$MOCK/half1.sh" > "$VERDICT_OUT" 2>&1
cat "$VERDICT_OUT"

LINKED=no
[ -e "$CONFIGFS/g1/configs/c.1/acm.usb0" ] && LINKED=yes
BOUND=no
[ -s "$CONFIGFS/g1/UDC" ] && BOUND=yes
CLAIMED=$(grep -c 'USB serial function is linked into the configuration' "$VERDICT_OUT")
CLAIMED_NOT=$(grep -c 'USB serial function is NOT linked' "$VERDICT_OUT")

echo
echo "actual: linked=$LINKED bound=$BOUND  |  reported: linked=$CLAIMED not-linked=$CLAIMED_NOT"
if [ "$LINKED" = yes ] && [ "$CLAIMED" -eq 1 ] && [ "$CLAIMED_NOT" -eq 0 ]; then
	echo "VERDICT: correct -- it reported the state that is actually there"
elif [ "$LINKED" = no ] && [ "$CLAIMED" -eq 0 ] && [ "$CLAIMED_NOT" -eq 1 ]; then
	echo "VERDICT: correct -- it reported the state that is actually there"
else
	echo "VERDICT: WRONG -- the report does not match the state"
	exit 1
fi

if [ "$LINKED" = yes ] && [ "$BOUND" = yes ]; then
	echo "RESULT: acm.usb0 IS linked into c.1 and the controller IS bound"
	exit 0
fi
if [ "$LINKED" = yes ]; then
	echo "RESULT: acm.usb0 is linked but the controller is NOT bound"
	exit 1
fi
echo "RESULT: acm.usb0 is NOT linked into c.1"
exit 1
