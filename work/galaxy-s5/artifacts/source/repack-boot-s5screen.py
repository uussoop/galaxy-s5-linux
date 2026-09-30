#!/usr/bin/env python3
"""Add the s5screen on-screen status and the check_keys hardening to the BOOT image.

This builds on the verified device-mapper bypass: the base is the same
subpartfix BOOT image that image was built from, so this image differs from the
known-good candidate by exactly one step - the marked regions in
init_functions.sh plus one new file, usr/bin/s5screen.

Properties asserted here, in order:

  * the base file, its kernel, its ramdisk and its device tree are exactly the
    bytes the dm-bypass step was verified against;
  * stripping every marked region from the replacement init_functions.sh
    reproduces the base's own init_functions.sh byte for byte, so the shell
    changes are confined to the marked regions and nothing else moved;
  * usr/bin/s5screen is a stripped static ARM executable;
  * the r1 kernel, the DT, the CACHE diagnostic logger and the losetup
    fallback are untouched;
  * the result still fits the BOOT partition.
"""

import gzip
import hashlib
import io
import re
import stat
import struct
import subprocess
import sys
from pathlib import Path

# Identical to repack-boot-dmsubpart.py: the same verified base.
BASE_SHA256 = "35de59d0d876747e6e00ff7a63887af92e5c02baf3614a075781795a55094a60"
BASE_RAMDISK_SHA256 = "20df4ee0efec141b6b0d53744fbc697a615ec648008fc2b8e30b90233b309e98"
KERNEL_SHA256 = "6d0c337411dcbfe6771347446af8fcab19bb5bff265496a5cddaa272de631b8d"
DT_SHA256 = "f6aac8da35c945bc982d10fccf29535a4692ea50669f5d323493c9c8b1228f6b"
BOOT_LIMIT = 13_631_488

TARGET = "init_functions.sh"
NEW_FILE = "usr/bin/s5screen"

# Every marked region that has to be strippable back out. Must stay in sync
# with the markers written by patch-init-functions.py.
REGIONS = [
    ("# >>> dm-bypass: helpers (begin)", "# <<< dm-bypass: helpers (end)"),
    ("# >>> s5screen: helpers (begin)", "# <<< s5screen: helpers (end)"),
    ("# >>> s5screen: booting hook (begin)", "# <<< s5screen: booting hook (end)"),
    ("# >>> s5screen: dmok hook (begin)", "# <<< s5screen: dmok hook (end)"),
    ("# >>> s5screen: dmno hook (begin)", "# <<< s5screen: dmno hook (end)"),
    (
        "# >>> dm-bypass: mount_subpartitions call (begin)",
        "# <<< dm-bypass: mount_subpartitions call (end)",
    ),
    ("# >>> s5screen: loopfail hook (begin)", "# <<< s5screen: loopfail hook (end)"),
    ("# >>> s5screen: booted hook (begin)", "# <<< s5screen: booted hook (end)"),
    (
        "# >>> s5screen: check_keys guard (begin)",
        "# <<< s5screen: check_keys guard (end)",
    ),
    (
        "# >>> s5screen: check_keys guard tail (begin)",
        "# <<< s5screen: check_keys guard tail (end)",
    ),
]


def align(n, block=4):
    return (n + block - 1) // block * block


def region_spans(data: bytes):
    """Locate every declared region as a (begin_line, end_line) pair."""
    lines = data.splitlines(keepends=True)
    spans = []
    for begin, end in REGIONS:
        b = begin.encode()
        e = end.encode()
        bi = next((i for i, l in enumerate(lines) if l.rstrip(b"\n").endswith(b)), None)
        if bi is None:
            raise SystemExit(f"region begin marker missing: {begin}")
        ei = next(
            (i for i in range(bi + 1, len(lines)) if lines[i].rstrip(b"\n").endswith(e)),
            None,
        )
        if ei is None:
            raise SystemExit(f"region end marker missing or out of order: {begin}")
        spans.append((bi, ei))
    return spans


def strip_regions(data: bytes) -> bytes:
    """Drop each outermost marked region, including the whole marker line, so
    what is left is the file the regions were inserted into. Regions may nest:
    the outermost one found swallows the inner ones, which is what we want, so
    only outermost regions are counted."""
    lines = data.splitlines(keepends=True)
    out, i, regions = [], 0, 0
    while i < len(lines):
        line = lines[i].rstrip(b"\n")
        begin = next((b for b, _ in REGIONS if line.endswith(b.encode())), None)
        if begin is None:
            out.append(lines[i])
            i += 1
            continue
        end = next(e for b, e in REGIONS if b == begin)
        j = i + 1
        while j < len(lines) and not lines[j].rstrip(b"\n").endswith(end.encode()):
            j += 1
        assert j < len(lines), f"unterminated region {begin!r}"
        regions += 1
        i = j + 1

    spans = region_spans(data)
    assert len(spans) == len(REGIONS)
    outermost = sum(
        1
        for i, (bi, ei) in enumerate(spans)
        if not any(bj < bi and ei < ej for j, (bj, ej) in enumerate(spans) if j != i)
    )
    assert regions == outermost, (
        f"stripped {regions} regions but {outermost} are outermost; if these "
        "differ the markers and REGIONS have gone out of step"
    )
    return b"".join(out)



def cpio_newc(name: str, data: bytes, ino: int, mode: int, mtime: int) -> bytes:
    """One newc (SVR4 with CRC) entry, 4-byte aligned, with no data padding
    beyond what the format requires."""
    name_nul = name.encode() + b"\0"
    namesize = len(name_nul)
    header_len = 110 + namesize
    pad = align(header_len) - header_len
    fields = [
        ino, mode, 0, 0, 1, mtime, len(data),
        0, 0, 0, 0, namesize, 0,
    ]
    out = b"070701" + b"".join(f"{f:08x}".encode() for f in fields)
    assert len(out) == 110, len(out)
    out += name_nul + b"\0" * pad
    out += data
    return out


def parse(raw):
    """Yield (name, header, data_start, data_size, end) for every entry."""
    pos = 0
    while pos < len(raw):
        header = raw[pos : pos + 110]
        assert header[:6] == b"070701", f"bad cpio magic at {pos}"
        f = [int(header[6 + n * 8 : 14 + n * 8], 16) for n in range(13)]
        name_end = pos + 110 + f[11]
        name = raw[pos + 110 : name_end - 1].decode()
        data_start = align(name_end)
        end = align(data_start + f[6])
        yield name, header, f, data_start, f[6], end
        pos = end
        if name == "TRAILER!!!":
            break


def check_s5screen(blob: bytes):
    """Refuse anything that is not a small, stripped, static ARM EABI5
    executable. Everything here is a claim about the binary that the
    initramfs is about to carry, so each is checked rather than assumed."""
    assert blob[:4] == b"\x7fELF", "s5screen is not an ELF file"
    assert blob[4] == 1, "s5screen is not ELF32"
    assert blob[5] == 1, "s5screen is not little-endian"
    e_type, e_machine = struct.unpack_from("<HH", blob, 16)
    assert e_machine == 40, f"s5screen is not ARM (e_machine={e_machine})"
    assert e_type == 2, f"s5screen is not an executable (e_type={e_type})"
    e_flags = struct.unpack_from("<I", blob, 36)[0]
    assert e_flags & 0xFF000000 == 0x05000000, (
        f"s5screen is not EABI5 (e_flags={e_flags:#x})"
    )
    # A static build never has an interpreter, which is what lets it run
    # before the real root filesystem is mounted.
    assert b"ld-musl" not in blob and b"/lib/ld" not in blob, (
        "s5screen looks dynamically linked"
    )
    # Stripped: no debug or symbol tables, so the size is what it needs to be.
    for junk in (b".debug_info", b".debug_str", b".symtab", b".strtab"):
        assert junk not in blob, f"s5screen still contains {junk.decode()}"
    assert len(blob) < 64 * 1024, f"s5screen unexpectedly large: {len(blob)} B"



def run(base_path, functions_path, s5screen_path, output_path):
    base = base_path.read_bytes()
    assert hashlib.sha256(base).hexdigest() == BASE_SHA256, "base image changed"
    assert base[:8] == b"ANDROID!"
    kernel_size, _, ramdisk_size, _, second_size, _, _, page_size, dt_size, _ = struct.unpack_from("<10I", base, 8)
    assert second_size == 0 and page_size == 2048
    kernel_off = page_size
    ramdisk_off = kernel_off + align(kernel_size, page_size)
    dt_off = ramdisk_off + align(ramdisk_size, page_size)
    kernel = base[kernel_off : kernel_off + kernel_size]
    ramdisk = base[ramdisk_off : ramdisk_off + ramdisk_size]
    dt = base[dt_off : dt_off + dt_size]
    assert hashlib.sha256(kernel).hexdigest() == KERNEL_SHA256
    assert hashlib.sha256(ramdisk).hexdigest() == BASE_RAMDISK_SHA256
    assert hashlib.sha256(dt).hexdigest() == DT_SHA256

    replacement = functions_path.read_bytes()
    s5blob = s5screen_path.read_bytes()
    check_s5screen(s5blob)

    raw = gzip.decompress(ramdisk)
    entries = list(parse(raw))
    names = [e[0] for e in entries]
    assert NEW_FILE not in names, f"{NEW_FILE} already exists in the base initramfs"
    assert names.count(TARGET) == 1, f"expected exactly one {TARGET} entry"
    base_target = next(e for e in entries if e[0] == TARGET)
    base_functions = raw[base_target[3] : base_target[3] + base_target[4]]

    stripped = strip_regions(replacement)
    assert stripped == base_functions, (
        f"{TARGET} is not the base file plus the marked regions "
        f"({len(stripped)} stripped vs {len(base_functions)} base bytes)"
    )
    assert len(replacement) > len(base_functions), "the patch added no bytes"
    for begin, end in REGIONS:
        assert replacement.count(begin.encode()) == 1, begin
        assert replacement.count(end.encode()) == 1, end
    # A marked region must be introduced by a begin and closed by its own end.
    assert replacement.find(b">>> s5screen: helpers (begin)") < replacement.find(
        b"<<< s5screen: helpers (end)"
    )

    # The shell must still parse, and the helper must be defined before use.
    subprocess.run(["bash", "-n", str(functions_path)], check=True)
    t = replacement.decode()
    assert t.index("s5status() {") < t.index("s5status booting")
    for state in ("booting", "dmok", "dmno", "loopfail", "keys", "booted"):
        assert f"s5status {state}" in t, f"no call site paints the {state!r} state"
    assert "iskey KEY_LEFTSHIFT KEY_VOLUMEUP; then" in t, "check_keys changed shape"
    assert "keys_still_held KEY_LEFTSHIFT KEY_VOLUMEUP" in t

    # Rebuild the archive: replace init_functions.sh in place, and add
    # s5screen just before the TRAILER record.
    out = bytearray()
    pos = 0
    replaced = added = 0
    max_ino = max(f[0] for _, _, f, _, _, _ in entries)
    mtime = 0
    for name, header, f, data_start, size, end in entries:
        if name == TARGET:
            new_header = bytearray(header)
            new_header[54:62] = f"{len(replacement):08x}".encode()
            out.extend(new_header)
            out.extend(raw[pos + 110 : data_start])
            out.extend(replacement)
            out.extend(b"\0" * (align(len(replacement)) - len(replacement)))
            replaced += 1
        elif name == "TRAILER!!!":
            out.extend(
                cpio_newc(
                    NEW_FILE,
                    s5blob,
                    max_ino + 1,
                    stat.S_IFREG | 0o755,
                    mtime,
                )
            )
            out.extend(raw[pos:end])
            added += 1
        else:
            out.extend(raw[pos:end])
        pos = end
    assert replaced == 1 and added == 1, (replaced, added)
    cpio = bytes(out)

    stream = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=stream, compresslevel=9, mtime=0) as gz:
        gz.write(cpio)
    new_ramdisk = stream.getvalue()

    header = bytearray(base[:page_size])
    struct.pack_into("<I", header, 16, len(new_ramdisk))
    digest = hashlib.sha1()
    for part in (kernel, new_ramdisk, b"", dt):
        digest.update(part)
        digest.update(struct.pack("<I", len(part)))
    header[576:596] = digest.digest()
    image = bytes(header)
    for part in (kernel, new_ramdisk, dt):
        image += part + b"\0" * (align(len(part), page_size) - len(part))
    assert len(image) <= BOOT_LIMIT, f"image {len(image)} exceeds BOOT {BOOT_LIMIT}"
    if output_path.exists():
        output_path.unlink()
    output_path.write_bytes(image)

    print(f"image      {hashlib.sha256(image).hexdigest()}  {len(image)} B  {output_path}")
    print(f"ramdisk    {hashlib.sha256(new_ramdisk).hexdigest()}  {len(new_ramdisk)} B")
    print(f"kernel     {hashlib.sha256(kernel).hexdigest()}  (unchanged, r1)")
    print(f"device tree{hashlib.sha256(dt).hexdigest()}  (unchanged)")
    print(f"{TARGET} {len(base_functions)} -> {len(replacement)} B, "
          f"all {len(REGIONS)} regions strip back to the base")
    print(f"{NEW_FILE} {len(s5blob)} B, stripped static ARM, added")
    print(f"headroom   {BOOT_LIMIT - len(image)} B of {BOOT_LIMIT}")


if __name__ == "__main__":
    if len(sys.argv) != 5:
        raise SystemExit(
            "usage: repack-boot-s5screen.py base-boot.img init_functions.sh "
            "s5screen output.img"
        )
    run(*(Path(x) for x in sys.argv[1:]))
