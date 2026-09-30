#!/bin/sh
#
# get-lvgl.sh - fetch the exact LVGL source lvterm is built from, and record its
#               hash.
#
# The commit is not a choice, it is a pin: it is the one pmaports records in
# buffybox's APKBUILD, which is postmarketOS's own LVGL based phone UI. Building
# against the same version the distribution already builds against means any
# difference we hit is ours and not a version skew.
#
# The tarball's sha512 is checked against the value in that APKBUILD, so a
# corrupted or substituted download fails here rather than at compile time.
#
# usage: ./get-lvgl.sh
set -eu

here=$(cd "$(dirname "$0")" && pwd)
work="$here/../scratch-lvgl"

COMMIT=85aa60d18b3d5e5588d7b247abf90198f07c8a63
URL="https://github.com/lvgl/lvgl/archive/$COMMIT.tar.gz"
# From work/galaxy-s5/src/pmaports/main/buffybox/APKBUILD
SHA512=b2a7c72e81cb60c9eb059bb7fb1f4a3765586941222591addc8f493af0bd45b5412fa24fb875a9878b2a8e8c16486b20c6d1715a6cd3ae26c9825eb9ad1f86c6

mkdir -p "$work"
cd "$work"

if [ ! -f lvgl.tar.gz ]; then
    echo "fetching LVGL $COMMIT"
    curl -sSL -o lvgl.tar.gz "$URL"
fi

have=$(sha512sum lvgl.tar.gz 2>/dev/null | cut -d' ' -f1 || shasum -a 512 lvgl.tar.gz | cut -d' ' -f1)
if [ "$have" != "$SHA512" ]; then
    echo "sha512 MISMATCH"
    echo "  expected $SHA512"
    echo "  got      $have"
    exit 1
fi
echo "sha512 matches the value pinned in buffybox's APKBUILD"

rm -rf pristine patched "lvgl-$COMMIT"
tar xzf lvgl.tar.gz
mv "lvgl-$COMMIT" pristine
cp -R pristine patched

echo "version: $(grep -A2 'define LVGL_VERSION_MAJOR' pristine/lv_version.h | tr -d ' \t' | tr '\n' ' ')"
echo
echo "next: python3 $here/apply-lvgl-patches.py $work/patched"
echo "then: cd $here && make"
