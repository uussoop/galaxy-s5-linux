#!/bin/sh
# Test suite for the r17 USB serial gadget change.
#
# Each case runs diagnostics/mock_usb_gadget_r17.sh, which executes the real
# shipped setup_usb_network_configfs against a mock configfs that enforces the
# one kernel rule the fix turns on: a function cannot be added to a
# configuration whose controller is already bound.
#
# The mutants below are the regressions this suite exists to catch, so a future
# edit that reintroduces any of them fails here rather than on the phone.
#
#   $1  r17-ramdisk-init_functions.sh   (default: the one in artifacts/source)
#
# Exits non-zero if any case behaves differently from what is required.

set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/../artifacts/source"
R17="${1:-$SRC/r17-ramdisk-init_functions.sh}"
MOCK="$HERE/mock_usb_gadget_r17.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

[ -f "$R17" ] || { echo "no such file: $R17" >&2; exit 2; }
[ -f "$MOCK" ] || { echo "no such file: $MOCK" >&2; exit 2; }

pass=0
fail=0

# Mutants are always built from the canonical r17, never from the file named on
# the command line. Deriving them from the file under test means a structurally
# broken input cannot even be scored, and the regression fixtures would move
# whenever the source moved, so they would stop being regressions. The argument
# therefore controls only case 1.
CANON="$SRC/r17-ramdisk-init_functions.sh"
[ -f "$CANON" ] || { echo "no canonical r17 at $CANON" >&2; exit 2; }

# Generate every mutant in one Python block, because doing it from the shell
# means quoting the shell text under test, and a mis-quoted anchor silently
# yields no mutant -- which then makes every case pass for the wrong reason.
python3 - "$CANON" "$WORK" <<'PY'
import sys
from pathlib import Path

src, work = Path(sys.argv[1]), Path(sys.argv[2])
base = src.read_text()

B = "# >>> s5screen: usb serial function (begin)\n"
E = "# <<< s5screen: usb serial function (end)\n"
UDC_WRITE = '\tif [ -z "$skip_udc" ]; then\n\t\tsetup_usb_configfs_udc\n\tfi\n'
LINK_TEST = '\tif [ -e "$CONFIGFS/g1/configs/c.1/$CONFIGFS_ACM_FUNCTION" ]; then'
DEV_TEST = '\tif [ -e "$FAKE_TTYGS" ]; then'


def once(text, what):
    n = text.count(what)
    if n != 1:
        sys.exit(f"anchor for {what!r} matched {n} times, wanted exactly 1")
    return text


def write(name, text):
    # A mutant that is identical to the source tests nothing, and would let a
    # broken generation step masquerade as a passing regression test.
    if text == base:
        sys.exit(f"mutant {name} is identical to {src.name}")
    (work / name).write_text(text)
    print(f"  built mutant {name}")


# C: the r17 region moved after the controller is bound -- the ordering r16
# used, and the regression this whole change is about.
s = base.index(B)
e = base.index(E) + len(E)
region = base[s:e]
rest = base[:s] + base[e:]
write("mutant-late.sh", once(rest, UDC_WRITE).replace(
    UDC_WRITE, UDC_WRITE + region, 1))

# D: r17's ordering kept, but the check reverted to r16's /dev/ttyGS0 test.
write("mutant-devtest.sh", once(base, LINK_TEST).replace(LINK_TEST, DEV_TEST, 1))

# E: the exact r16 failure -- late ordering, the /dev/ttyGS0 test, and the
# driver having created that node on load. That combination produced
# "USB serial gadget is up, /dev/ttyGS0 exists" on the r16 boot, where the host
# had enumerated no USB device at all.
write("mutant-r16replica.sh", once(
    (work / "mutant-late.sh").read_text(), LINK_TEST).replace(
    LINK_TEST, DEV_TEST, 1))
PY
[ $? -eq 0 ] || { echo "mutant generation failed" >&2; exit 2; }

# --- the cases --------------------------------------------------------------
#
# want: pass or fail. note: what the case is actually asserting.

check() {
	name="$1"
	file="$2"
	want="$3"
	note="$4"
	simulate_ttygs="$5"

	# A missing case file would make the mock fail, which is indistinguishable
	# from a case that legitimately fails. Refuse to score it.
	if [ ! -f "$file" ]; then
		fail=$((fail + 1))
		printf 'FAIL  %-46s %s (case file missing: %s)\n' "$name" "$note" "$file"
		return
	fi

	if [ "$simulate_ttygs" = 1 ]; then
		SIMULATE_TTYGS=1 sh "$MOCK" "$file" > "$WORK/out" 2>&1
	else
		sh "$MOCK" "$file" > "$WORK/out" 2>&1
	fi
	rc=$?
	got=fail
	[ "$rc" -eq 0 ] && got=pass

	# Confirm the case produced the state it is supposed to produce, so a mock
	# that stopped exercising the code cannot report success.
	if [ "$want" = pass ]; then
		if ! grep -q 'VERDICT: correct' "$WORK/out"; then
			got=fail
			note="$note (verdict did not check out)"
		fi
	elif ! grep -Eq 'RESULT: acm.usb0 is NOT linked|VERDICT: WRONG' "$WORK/out"; then
		got=pass
		note="$note (case did not reach the expected state; result is meaningless)"
	fi

	if [ "$got" = "$want" ]; then
		pass=$((pass + 1))
		printf 'PASS  %-46s %s\n' "$name" "$note"
	else
		fail=$((fail + 1))
		printf 'FAIL  %-46s %s\n' "$name" "$note"
		printf '        wanted %s, got %s, exit %s\n' "$want" "$got" "$rc"
		sed 's/^/        | /' "$WORK/out" | tail -25
	fi
}

check "r17" "$R17" pass \
	"acm linked before the bind, controller bound, verdict true" 0
check "r16 ordering (acm added after the bind)" "$WORK/mutant-late.sh" fail \
	"kernel refuses the function, nothing linked" 0
check "r16 test (/dev/ttyGS0) on r17 ordering" "$WORK/mutant-devtest.sh" fail \
	"wrong test cannot find the link" 0
check "r16 replica: late + /dev/ttyGS0 + node present" \
	"$WORK/mutant-r16replica.sh" fail \
	"false positive: reports linked when nothing is linked" 1

if [ -f "$SRC/r16-ramdisk-init_functions.sh" ]; then
	check "r16 image source" "$SRC/r16-ramdisk-init_functions.sh" fail \
		"no early acm at all" 0
else
	printf 'SKIP  %-46s %s\n' "r16 image source" "file not present"
fi

echo
echo "passed $pass, failed $fail"
[ "$fail" -eq 0 ] || exit 1
echo "all cases behaved as required"
