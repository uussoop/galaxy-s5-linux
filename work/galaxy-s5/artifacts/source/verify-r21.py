#!/usr/bin/env python3
"""Structural and content verification for the r21 boot image.

The r20 image bricked the phone while flashing cleanly, reading back with a
matching md5, and passing a tolerant cpio parse. The defect was structural --
cpio_newc padded each record's name to 4 bytes but not its data, and the new
keeper was 7265 B (mod 4 == 1), so every following record shifted by one byte
and the kernel's unpacker never reached TRAILER. Content checks cannot see that.
So this walks the archive itself, refusing to proceed past anything that is not
exactly right, and only then compares the bytes that matter.
"""
import hashlib
import importlib.util
import pathlib
import struct
import sys

HERE = pathlib.Path(__file__).resolve().parent
AL = lambda n, l: n + ((-n) % l)  # noqa: E731  round up, and 0 stays 0


def walk(cpio: bytes, label: str):
    """Strict newc walk. No tolerance at all: stop at the first oddity."""
    off = 0
    out = []
    while True:
        if cpio[off:off + 6] != b"070701":
            raise AssertionError(f"{label}: bad cpio magic at {off}: {cpio[off:off + 6]!r}")
        # newc stores every field as 8 hex characters, so the fields are not
        # 4-byte integers and struct cannot be used here. Byte offsets:
        #   6 + 8n  c_ino, c_mode, c_uid, c_gid, c_nlink, c_mtime, c_filesize,
        #           c_devmajor, c_devminor, c_rdevmajor, c_rdevminor,
        #           c_namesize, c_check
        h = cpio[off : off + 110]
        filesize = int(h[54:62], 16)
        namesize = int(h[94:102], 16)
        if namesize == 0:
            raise AssertionError(f"{label}: zero-length name at offset {off}")
        hdr = 110 + namesize
        name = cpio[off + 110:off + hdr].rstrip(b"\0").decode("utf-8", "strict")
        if name == "TRAILER!!!":
            return out, off + AL(hdr, 4)
        data_at = off + AL(hdr, 4)
        data_end = data_at + filesize
        if data_end > len(cpio):
            raise AssertionError(f"{label}: {name} overruns the buffer")
        out.append((name, cpio[data_at:data_end]))
        off = data_at + AL(filesize, 4)
        if len(out) > 100000:
            raise AssertionError(f"{label}: runaway archive")


def main() -> int:
    spec = importlib.util.spec_from_file_location("b", HERE / "repack-boot-r21.py")
    b = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(b)

    grabbed = {}
    orig = b.cpio_walk_strict
    b.cpio_walk_strict = lambda c, l: (grabbed.__setitem__("c", c), orig(c, l))[1]
    P = pathlib.Path
    b.run(
        P("boot-k3gxx-r11-subpartfix-35de59d0.img"),
        P("source/r19-ramdisk-init_functions.sh"),
        P("source/vmlinuz-fbcon"),
        {
            "usr/bin/s5screen": P("source/s5screen"),
            "usr/bin/s5iskey": P("source/s5iskey"),
            "usr/bin/s5usbkeep": P("source/diagnostic-ramdisk-r21/s5usbkeep"),
        },
        P("boot-k3gxx-r21-cachelog.img"),
    )
    cpio = grabbed["c"]
    entries, trailer_at = walk(cpio, "r21 archive")
    names = dict(entries)
    print(f"strict walk: {len(entries)} entries, TRAILER at {trailer_at}, archive {len(cpio)} B\n")

    bad = 0
    for want, path in [
        ("usr/bin/s5usbkeep", "source/diagnostic-ramdisk-r21/s5usbkeep"),
        ("init_2nd.sh", "source/diagnostic-ramdisk-r21/init_2nd.sh"),
        ("usr/bin/s5diag", "source/diagnostic-ramdisk-r21/s5diag"),
    ]:
        src = P(path).read_bytes()
        got = names[want]
        ok = src == got
        bad += not ok
        print(f"  {'OK  ' if ok else 'DIFF'} {want:18s} {len(got):6d} B  sha={hashlib.sha256(got).hexdigest()[:16]}")

    i2, keep, diag = names["init_2nd.sh"], names["usr/bin/s5usbkeep"], names["usr/bin/s5diag"]
    print()
    for label, ok in [
        ("init_2nd.sh defines and calls s5usbkeep_install", b"\ns5usbkeep_install\n" in i2),
        ("init_2nd.sh hands over, never stops", b"s5diag handover" in i2 and b"s5diag stop" not in i2),
        ("init_2nd.sh repairs an inittab with no trailing newline", b"did not end in a newline" in i2),
        ("init_2nd.sh reads the install back off the real root (7 lines)", i2.count(b"verify:") == 7),
        ("init_2nd.sh syntax-checks the keeper it installed", b"sh -n" in i2),
        ("keeper logs to /dev/kmsg, the sink that survives a warm reboot", b"> /dev/kmsg" in keep),
        ("keeper logs to CACHE, under an r21 label", b"CACHE_LOGDIR=codex-s5-diagnostics/r21" in keep),
        ("keeper logs to /var/log", b"/var/log/s5usbkeep.log" in keep),
        ("keeper tries 3 CACHE device paths", b"mmcblk0p20" in keep),
        ("keeper tries 3 CACHE mount points", b"/var/tmp/s5keep" in keep),
        ("keeper records a preflight before touching sysfs", b"preflight() {" in keep),
        ("keeper logs the ring buffer before each re-assert", b"save_pre_state()" in keep),
        ("s5diag handover flag is no longer on /run", b"handoverfile=/run/s5diag.handover" not in diag),
        ("s5diag adds probe() naming the facilities that survived", b"probe() {" in diag),
        ("s5diag flags the handover on CACHE", b"handover: flagged" in diag),
        ("keeper is 7575 B, so data padding is load-bearing (mod 4 == 3)", len(keep) % 4 == 3),
    ]:
        bad += not ok
        print(f"  {'OK  ' if ok else 'FAIL'} {label}")

    img = P("boot-k3gxx-r21-cachelog.img").read_bytes()
    print(f"\nimage {len(img)} B  md5 {hashlib.md5(img).hexdigest()}")
    print(f"sha256 {hashlib.sha256(img).hexdigest()}")
    print(f"\n{len(entries)} entries walked, {16 - bad} of 16 content checks pass" if not bad else f"\n{bad} FAILURES")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
