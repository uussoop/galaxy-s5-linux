#!/sbin/sh
# Read-only USERDATA loop probe. Run from TWRP shell; this script never mounts or repairs.
set -u
R=/tmp/codex-s5-native
C=/tmp/codex-s5-maint/chroot
LO="$R/lib/ld-musl-armhf.so.1"
SRC=/dev/block/mmcblk0p21
BYNAME=/dev/block/platform/12200000.dwmmc0/by-name/USERDATA
LOOP=/dev/block/loop7
SYS=/sys/block/loop7
EXPECTED=9ab4ada0e4d08037da6a6b350bc498990f36905cefd043086718721da00303ed
owned=0
scratch=
fail() { echo "ABORT: $*" >&2; exit 1; }
bb() { "$C/lib/ld-musl-armhf.so.1" --library-path "$C/lib" "$C/bin/busybox" "$@"; }
losetup_native() { "$LO" --library-path "$R/lib:$R/usr/lib" "$R/usr/bin/losetup" "$@"; }
empty_loop() {
  [ "$(cat "$SYS/size")" = 0 ] || fail "loop7 size is nonzero"
  [ ! -e "$SYS/loop/backing_file" ] || fail "loop7 already has a backing file"
}
cleanup() {
  if [ "$owned" = 1 ]; then losetup_native -d "$LOOP" || echo 'WARNING: loop7 detach failed' >&2; owned=0; fi
  [ -z "$scratch" ] || bb rm -f "$scratch"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
[ -b "$SRC" ] || fail "source missing"
[ -b "$LOOP" ] || fail "loop7 missing"
[ "$(bb stat -c '%t:%T' "$LOOP")" = 7:7 ] || fail "loop7 device number mismatch"
[ "$(bb readlink -f "$BYNAME")" = "$(bb readlink -f "$SRC")" ] || fail "USERDATA by-name mismatch"
[ "$(cat /sys/class/block/mmcblk0p21/partition)" = 21 ] || fail "source partition number mismatch"
[ "$(cat /sys/class/block/mmcblk0p21/start)" = 5918720 ] || fail "source start mismatch"
[ "$(cat /sys/class/block/mmcblk0p21/size)" = 24846336 ] || fail "source size mismatch"
[ -x "$LO" ] && [ -x "$R/usr/bin/losetup" ] && [ -x "$C/bin/busybox" ] || fail "native utilities missing"
[ "$(bb sha256sum "$R/usr/bin/losetup" | bb cut -d ' ' -f 1)" = 074036ffe027c942212e448668d1bb176b461a3a70e1b293ad7c74d2ec2895c7 ] || fail "losetup binary hash mismatch"
empty_loop
scratch="$(bb mktemp /tmp/s5-loop-probe.XXXXXX)" || fail "scratch creation failed"
bb dd if="$SRC" of="$scratch" bs=512 count=1 2>/dev/null || fail "source sector read failed"
[ "$(bb sha256sum "$scratch" | bb cut -d ' ' -f 1)" = "$EXPECTED" ] || fail "source sector hash mismatch"
echo 'Source guard passed; probing loop7 read-only.'
for mode in on off; do
  empty_loop
  owned=1
  if ! losetup_native --read-only -P --direct-io="$mode" "$LOOP" "$SRC"; then
    echo "direct-io=$mode attach failed"
    if [ "$(cat "$SYS/size")" != 0 ]; then
      losetup_native -d "$LOOP" || fail "partial loop7 attach could not detach"
    fi
    owned=0
    empty_loop
    continue
  fi
  [ "$(cat "$SYS/ro")" = 1 ] || fail "loop7 is not read-only"
  if bb dd if="$LOOP" of="$scratch" bs=512 count=1 2>/dev/null && [ "$(bb stat -c '%s' "$scratch")" = 512 ]; then
    echo "direct-io=$mode first512=$(bb sha256sum "$scratch" | bb cut -d ' ' -f 1)"
  else
    echo "direct-io=$mode first512 read failed"
  fi
  for p in loop7p1 loop7p2; do
    if [ -e "/sys/class/block/$p/start" ] && [ -e "/sys/class/block/$p/size" ]; then
      echo "$p start=$(cat "/sys/class/block/$p/start") size=$(cat "/sys/class/block/$p/size")"
    else
      echo "$p absent"
    fi
  done
  losetup_native -d "$LOOP" || fail "loop7 detach failed"
  owned=0
  empty_loop
done
