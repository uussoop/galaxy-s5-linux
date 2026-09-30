#!/usr/bin/env python3
"""Build the BOOT image that gives this device a framebuffer console.

The r12 boot proved the whole initramfs chain works: the device-mapper bypass
mounts the nested USERDATA partition, root is found and mounted, and
"Switching root" is reached. What it could not do is show any of it, because
this kernel's config has every part of the console stack compiled in
(CONFIG_VT, CONFIG_VT_CONSOLE, CONFIG_HW_CONSOLE, CONFIG_FB and the
CONFIG_FB_CFB_* text blitters) except the single glue layer that binds a VT
console to a framebuffer:

    # CONFIG_FRAMEBUFFER_CONSOLE is not set

So a successful boot and a frozen phone looked identical on the panel, and the
real system painted nothing. This image swaps in a kernel rebuilt with
CONFIG_FRAMEBUFFER_CONSOLE=y, which is a one-line config change; Kconfig's
select pulls in a VGA font automatically. The kernel is otherwise the same
source and the same config, and the device tree is byte-identical.

Alongside that, one initramfs change. keys_still_held asks s5iskey instead of
iskey. iskey aggregates every /dev/input/event* and cannot say which device
answered, and two nodes advertise KEY_VOLUMEUP here: gpio_keys.16, the real
key, and "Headset" (arizona-extcon), which synthesises volume keys while the
headphone-detect pin settles. That made the "hold left shift and volume up to
fail the boot" check fire on a key nobody pressed, halting the boot. s5iskey
skips the synthesising device, names the one that answered, and otherwise asks
the same question iskey does.

Properties asserted here, in order:

  * the base file, its ramdisk and its device tree are exactly the bytes the
    dm-bypass step was verified against;
  * the replacement kernel is exactly the expected rebuilt kernel;
  * stripping every marked region from the replacement init_functions.sh
    reproduces the base's own init_functions.sh byte for byte, so the shell
    changes are confined to the marked regions and nothing else moved;
  * both new files are small, stripped, static ARM EABI5 executables;
  * the CACHE diagnostic logger and the losetup fallback are untouched;
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
BASE_KERNEL_SHA256 = "6d0c337411dcbfe6771347446af8fcab19bb5bff265496a5cddaa272de631b8d"
DT_SHA256 = "f6aac8da35c945bc982d10fccf29535a4692ea50669f5d323493c9c8b1228f6b"
# The rebuilt kernel: same source and same config as the r1 kernel above, with
# the single CONFIG_FRAMEBUFFER_CONSOLE=y change, built by pmbootstrap from
# linux-samsung-k3gxx-3.10.9-r2. Asserted so a stray or truncated kernel cannot
# be packed by accident.
NEW_KERNEL_SHA256 = "4a559bc8b309dcd959db2d2d7d30cf2d3cdfeae2bf41a6c8e0579fa6e59d7377"
BOOT_LIMIT = 13_631_488

TARGET = "init_functions.sh"
# Both are static ARM helpers the initramfs runs before the real root exists.
NEW_FILES = ("usr/bin/s5screen", "usr/bin/s5iskey")

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
    (
        "# >>> s5screen: usb serial function (begin)",
        "# <<< s5screen: usb serial function (end)",
    ),
]


def align(n, block=4):
    return (n + block - 1) // block * block


def strip_shell_comment(line: str) -> str:
    """Drop a trailing shell comment, respecting simple quoting.

    Only used to decide whether code calls a command, so a '#' inside a quoted
    string must not be mistaken for the start of a comment. Unterminated quotes
    are treated as literal, which is the safe direction here: it keeps text that
    a reader would see as code.
    """
    out = []
    quote = None
    for i, ch in enumerate(line):
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
            out.append(ch)
            continue
        if ch == "#":
            # A '#' only starts a comment at the start of a word.
            if i == 0 or line[i - 1] in " \t":
                break
            out.append(ch)
            continue
        out.append(ch)
    return "".join(out)


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


def check_arm_static(name: str, blob: bytes):
    """Refuse anything that is not a small, stripped, static ARM EABI5
    executable. Everything here is a claim about the binary that the
    initramfs is about to carry, so each is checked rather than assumed."""
    assert blob[:4] == b"\x7fELF", f"{name} is not an ELF file"
    assert blob[4] == 1, f"{name} is not ELF32"
    assert blob[5] == 1, f"{name} is not little-endian"
    e_type, e_machine = struct.unpack_from("<HH", blob, 16)
    assert e_machine == 40, f"{name} is not ARM (e_machine={e_machine})"
    assert e_type == 2, f"{name} is not an executable (e_type={e_type})"
    e_flags = struct.unpack_from("<I", blob, 36)[0]
    assert e_flags & 0xFF000000 == 0x05000000, (
        f"{name} is not EABI5 (e_flags={e_flags:#x})"
    )
    # A static build never has an interpreter, which is what lets it run
    # before the real root filesystem is mounted.
    assert b"ld-musl" not in blob and b"/lib/ld" not in blob, (
        f"{name} looks dynamically linked"
    )
    # Stripped: no debug or symbol tables, so the size is what it needs to be.
    for junk in (b".debug_info", b".debug_str", b".symtab", b".strtab"):
        assert junk not in blob, f"{name} still contains {junk.decode()}"
    assert len(blob) < 64 * 1024, f"{name} unexpectedly large: {len(blob)} B"



def run(base_path, functions_path, kernel_path, helpers, output_path):
    base = base_path.read_bytes()
    assert hashlib.sha256(base).hexdigest() == BASE_SHA256, "base image changed"
    assert base[:8] == b"ANDROID!"
    kernel_size, _, ramdisk_size, _, second_size, _, _, page_size, dt_size, _ = struct.unpack_from("<10I", base, 8)
    assert second_size == 0 and page_size == 2048
    kernel_off = page_size
    ramdisk_off = kernel_off + align(kernel_size, page_size)
    dt_off = ramdisk_off + align(ramdisk_size, page_size)
    base_kernel = base[kernel_off : kernel_off + kernel_size]
    ramdisk = base[ramdisk_off : ramdisk_off + ramdisk_size]
    dt = base[dt_off : dt_off + dt_size]
    assert hashlib.sha256(base_kernel).hexdigest() == BASE_KERNEL_SHA256
    assert hashlib.sha256(ramdisk).hexdigest() == BASE_RAMDISK_SHA256
    assert hashlib.sha256(dt).hexdigest() == DT_SHA256

    # The one intended kernel change: swap in the rebuilt fbcon kernel. The
    # device tree is deliberately taken from the base, not from the new build,
    # so the DT cannot drift even though the new build also emitted one.
    kernel = kernel_path.read_bytes()
    assert hashlib.sha256(kernel).hexdigest() == NEW_KERNEL_SHA256, (
        "replacement kernel is not the expected fbcon rebuild"
    )

    replacement = functions_path.read_bytes()
    blobs = {}
    for name, path in helpers.items():
        blob = path.read_bytes()
        check_arm_static(name, blob)
        blobs[name] = blob

    raw = gzip.decompress(ramdisk)
    entries = list(parse(raw))
    names = [e[0] for e in entries]
    for nf in NEW_FILES:
        assert nf not in names, f"{nf} already exists in the base initramfs"
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
    # The guard must ask s5iskey, which skips the synthesising headset device,
    # and must not have quietly gone back to iskey inside the helper. Comments
    # are stripped first: the helper's own comment explains why iskey is not
    # used, and counting that word would make this check pass or fail on prose.
    body = t.split("keys_still_held() {")[1].split("\n# <<<")[0]
    code = "\n".join(strip_shell_comment(l) for l in body.splitlines())
    assert "s5iskey" in code, "keys_still_held does not use s5iskey"
    assert not re.search(r"(?<![A-Za-z0-9_])iskey(?![A-Za-z0-9_])", code), (
        "keys_still_held still calls iskey directly"
    )

    # Rebuild the archive: replace init_functions.sh in place, and add both
    # helpers just before the TRAILER record.
    out = bytearray()
    pos = 0
    replaced = 0
    added = 0
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
            for nf in NEW_FILES:
                out.extend(
                    cpio_newc(
                        nf,
                        blobs[nf],
                        max_ino + 1 + added,
                        stat.S_IFREG | 0o755,
                        mtime,
                    )
                )
                added += 1
            out.extend(raw[pos:end])
        else:
            out.extend(raw[pos:end])
        pos = end
    assert replaced == 1 and added == len(NEW_FILES), (replaced, added)
    cpio = bytes(out)

    stream = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=stream, compresslevel=9, mtime=0) as gz:
        gz.write(cpio)
    new_ramdisk = stream.getvalue()

    header = bytearray(base[:page_size])
    # The header must describe the parts that are actually written. The kernel
    # and the ramdisk both change size here, and the loader trusts these fields
    # to find them: leaving kernel_size at the old value makes the ramdisk and
    # the device tree overlap the tail of the new kernel, and the phone then
    # boots a truncated kernel. dt_size is restated even though the DT is
    # unchanged, so the header is a function of the parts rather than of which
    # parts happened to keep their size.
    struct.pack_into("<I", header, 8, len(kernel))
    struct.pack_into("<I", header, 16, len(new_ramdisk))
    struct.pack_into("<I", header, 40, len(dt))

    # The header cmdline is left exactly as the base has it, and that is a
    # finding rather than an oversight: on this device the bootloader ignores
    # the BOOT image's cmdline field entirely and passes only the device tree's
    # /chosen/bootargs. The r14 kernel log shows the whole 850-byte line the
    # kernel received, and it contains neither "quiet" nor "buildvariant=eng",
    # which are that field's entire contents. So the s5keys=n that gets this
    # image past the halt is set inside the initramfs, where it is read.

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

    # Re-read what was just written and confirm the header locates each part,
    # instead of trusting that the fields above were set consistently. This is
    # exactly the check the previous version of this tool lacked, and it is the
    # one that would have caught the stale kernel_size at build time.
    written = output_path.read_bytes()
    h = written[:page_size]
    h_ksize, _, h_rsize, _, h_ssize, _, _, h_page, h_dtsize, _ = struct.unpack_from(
        "<10I", h, 8
    )
    assert (h_ksize, h_rsize, h_ssize, h_page, h_dtsize) == (
        len(kernel),
        len(new_ramdisk),
        0,
        page_size,
        len(dt),
    ), "header sizes do not describe the written parts"
    k_off = h_page
    r_off = k_off + align(h_ksize, h_page)
    d_off = r_off + align(h_rsize, h_page)
    assert written[k_off : k_off + h_ksize] == kernel, "kernel is not where the header says"
    assert written[r_off : r_off + h_rsize] == new_ramdisk, "ramdisk is not where the header says"
    assert written[r_off : r_off + 2] == b"\x1f\x8b", "ramdisk is not a gzip stream"
    assert written[d_off : d_off + h_dtsize] == dt, "device tree is not where the header says"
    # Nothing may be written past the last part.
    assert len(written) == d_off + align(h_dtsize, h_page), (
        f"trailing bytes after the device tree: {len(written)} B written, "
        f"parts end at {d_off + align(h_dtsize, h_page)}"
    )
    recheck = hashlib.sha1()
    for part in (kernel, new_ramdisk, b"", dt):
        recheck.update(part)
        recheck.update(struct.pack("<I", len(part)))
    assert h[576:596] == recheck.digest(), "header SHA-1 does not match the parts"
    # Confirm the cmdline really is untouched. It is inert on this device, but
    # a future change to it should have to be a deliberate act, not a side
    # effect of a fix that does not work.
    written_cmdline = bytes(written[64:576]).split(b"\0")[0]
    base_cmdline = bytes(base[:page_size][64:576]).split(b"\0")[0]
    assert written_cmdline == base_cmdline, (
        f"header cmdline was modified: {base_cmdline!r} -> {written_cmdline!r}"
    )

    print(f"image      {hashlib.sha256(image).hexdigest()}  {len(image)} B  {output_path}")
    print(f"ramdisk    {hashlib.sha256(new_ramdisk).hexdigest()}  {len(new_ramdisk)} B")
    print(f"kernel     {hashlib.sha256(kernel).hexdigest()}  (rebuilt, fbcon)")
    print(f"device tree{hashlib.sha256(dt).hexdigest()}  (unchanged)")
    print(f"cmdline    {written_cmdline.decode()!r}")
    print(f"{TARGET} {len(base_functions)} -> {len(replacement)} B, "
          f"all {len(REGIONS)} regions strip back to the base")
    for nf in NEW_FILES:
        print(f"{nf} {len(blobs[nf])} B, stripped static ARM, added")
    print(f"headroom   {BOOT_LIMIT - len(image)} B of {BOOT_LIMIT}")


if __name__ == "__main__":
    if len(sys.argv) != 7:
        raise SystemExit(
            "usage: repack-boot-fbcon.py base-boot.img init_functions.sh "
            "vmlinuz s5screen s5iskey output.img"
        )
    (
        base_path,
        functions_path,
        kernel_path,
        s5screen_path,
        s5iskey_path,
        output_path,
    ) = (Path(x) for x in sys.argv[1:])
    run(
        base_path,
        functions_path,
        kernel_path,
        {
            "usr/bin/s5screen": s5screen_path,
            "usr/bin/s5iskey": s5iskey_path,
        },
        output_path,
    )
