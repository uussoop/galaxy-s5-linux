#!/usr/bin/env python3
"""Prove that the r20 boot image is r19 plus exactly one new file and one
contiguous block in init_2nd.sh, and nothing else.

This is the check that matters for r20 specifically, because r20's whole claim
is that it is a minimal, reviewable delta on top of a revision that has already
been flashed and read back. If the delta were wider than advertised -- a
different init_functions.sh, a changed s5diag, a silently rewritten base file --
then "r20" would be an untested revision wearing r19's name, which is exactly
the failure this series of checks exists to prevent.

    cd artifacts && python3 source/verify-r20-delta.py
"""

import gzip
import hashlib
import importlib.util
import pathlib
import struct
import sys

HERE = pathlib.Path(__file__).resolve().parent
ART = HERE.parent

spec = importlib.util.spec_from_file_location("rp", HERE / "repack-boot-r20.py")
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)


def ramdisk_of(img):
    raw = pathlib.Path(img).read_bytes()
    assert raw[:8] == b"ANDROID!", f"{img} is not an android boot image"
    (ks, _, rs, _, ss, _, _, ps, ds, _) = struct.unpack_from("<10I", raw, 8)
    off = ps + rp.align(ks, ps)
    return gzip.decompress(raw[off : off + rs])


def entries(cpio):
    out, pos = {}, 0
    while pos < len(cpio):
        if cpio[pos : pos + 6] != b"070701":
            pos += 1
            continue
        h = cpio[pos : pos + 110]
        fsize, nsize = int(h[54:62], 16), int(h[94:102], 16)
        name = cpio[pos + 110 : pos + 110 + nsize - 1].decode()
        d = pos + 110 + nsize
        d += (-d) % 4
        if name == "TRAILER!!!":
            break
        out[name] = cpio[d : d + fsize]
        pos = d + fsize
        pos += (-pos) % 4
    return out


def main():
    r19_img = ART / "boot-k3gxx-r19-cachelog-242091ca.img"
    r20_img = next(ART.glob("boot-k3gxx-r20-realsys-*.img"))
    print(f"  r19 {r19_img.name}")
    print(f"  r20 {r20_img.name}\n")

    r19 = entries(ramdisk_of(r19_img))
    r20 = entries(ramdisk_of(r20_img))

    added = sorted(set(r20) - set(r19))
    removed = sorted(set(r19) - set(r20))
    changed = sorted(n for n in set(r19) & set(r20) if r19[n] != r20[n])
    print(f"  added:   {added}")
    print(f"  removed: {removed}")
    print(f"  changed: {changed}\n")

    ok = True

    def check(label, cond):
        nonlocal ok
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")
        ok = ok and bool(cond)

    i2 = r20.get("init_2nd.sh", b"")
    keep = r20.get("usr/bin/s5usbkeep", b"")

    check("exactly one file added, and it is usr/bin/s5usbkeep",
          added == ["usr/bin/s5usbkeep"])
    check("nothing removed from the ramdisk", removed == [])
    check("only init_2nd.sh was modified", changed == ["init_2nd.sh"])
    check("s5diag is byte-identical to r19's",
          hashlib.sha256(r20["usr/bin/s5diag"]).hexdigest()
          == hashlib.sha256(r19["usr/bin/s5diag"]).hexdigest())
    check("init_functions.sh is byte-identical to r19's",
          hashlib.sha256(r20["init_functions.sh"]).hexdigest()
          == hashlib.sha256(r19["init_functions.sh"]).hexdigest())
    check("s5usbkeep matches its source file byte for byte",
          keep == (HERE / "diagnostic-ramdisk-r20" / "s5usbkeep").read_bytes())
    check("s5usbkeep has a #!/bin/sh shebang", keep.startswith(b"#!/bin/sh"))
    check("s5usbkeep is ASCII-clean, no CRLF", b"\r" not in keep)

    # The install block has to be defined once and called once, and the call has
    # to be before switch_root, or the revision boots cleanly and fixes nothing.
    check("s5usbkeep_install is defined exactly once",
          i2.count(b"s5usbkeep_install() {") == 1)
    check("s5usbkeep_install is called exactly once",
          i2.count(b"\ns5usbkeep_install\n") == 1)
    check("the call is before switch_root",
          i2.find(b"\ns5usbkeep_install\n") < i2.find(b"# Switch root"))

    # And cutting the block out has to give r19's init_2nd.sh back exactly.
    beg = i2.find(rp.INSTALL_BLOCK_BEGIN.encode())
    end = i2.find(rp.INSTALL_BLOCK_END.encode())
    check("the install block is locatable by its sentinels", beg != -1 and end > beg)
    if beg != -1 and end > beg:
        check("removing the block reproduces r19's init_2nd.sh byte for byte",
              i2[:beg] + i2[end:] == r19["init_2nd.sh"])
        print(f"  (the block is {end - beg} B at offset {beg})")

    # The keeper has to do the job, not merely exist.
    body = keep.decode()
    for needed in (
        'echo "acm" > "$ad/functions"',
        'echo "0" > "$ad/enable"',
        'echo "1" > "$ad/enable"',
        "while :",
        "dmesg",
        "/dev/mmcblk0p19",
        "s5usbkeep::respawn:/usr/sbin/s5usbkeep",
    ):
        if needed == "s5usbkeep::respawn:/usr/sbin/s5usbkeep":
            check(f"init_2nd.sh writes the inittab line: {needed}",
                  needed.encode() in i2)
        else:
            check(f"s5usbkeep contains {needed!r}", needed in body)

    print("\n  " + ("ALL PASS" if ok else "SOMETHING FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
