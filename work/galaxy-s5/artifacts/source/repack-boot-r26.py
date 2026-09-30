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

r19 adds nothing to init_functions.sh. It changes the CACHE logger and the two
lines in init_2nd.sh that shut that logger down, because every log so far ends
at "Switching root" and that is by design:

    /sbin/s5diag stop "$S5_DIAG_PID" || true      # init_2nd.sh, before switch_root

"s5diag stop" writes the stage marker, takes a final snapshot and unmounts
CACHE, so the r18 log stops dead at 7.457s. The acm function did bind correctly
in there -- "usb: acm is enabled. (bcdDevice=0x400)" and
"vendor=18d1,product=d001,bcdDevice=400", with no "Could not bind acm%u config"
anywhere -- yet the host ended up holding an ncm descriptor, which the
bcdDevice=ffff of the earlier ncm bind identifies exactly. Whatever replaced it
did so after the initramfs was gone, in a stretch of boot this logger discards.

So r19 hands the logger over instead of stopping it. "s5diag start" is a
background child of PID 1 forked before switch_root, and busybox switch_root only
moves the old root aside, so a process already holding that root keeps its own
/sys, /proc, /dev and its CACHE mount. The keeper outlives the initramfs, keeps
appending the ring buffer, and re-asserts acm on android_usb whenever it
drifts, recording what changed it and when. It cannot affect the boot: every
write is guarded, it only ever writes inside CACHE, and if the post-root half
cannot run at all the pre-root log is on CACHE exactly as before.

Properties asserted here, in order:

  * the base file, its ramdisk and its device tree are exactly the bytes the
    dm-bypass step was verified against;
  * the replacement kernel is exactly the expected rebuilt kernel;
  * stripping every marked region from the replacement init_functions.sh
    reproduces the base's own init_functions.sh byte for byte, so the shell
    changes are confined to the marked regions and nothing else moved;
  * both new files are small, stripped, static ARM EABI5 executables;
  * the losetup fallback is untouched;
  * the CACHE logger is kept but handed over at switch_root instead of stopped;
  * reversing the two init_2nd.sh edits reproduces the base file byte for byte;
  * with --no-diag the ramdisk is byte-identical to r18, so the r19 delta is
    exactly the two diagnostic files;
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
# Samsung wraps the flattened tree in a 2 KB "DTBH" blob header and DT_SIZE
# counts the wrapper. Everything below works on the tree after it.
DTBH_LEN = 2048
# Pinned so the patch below cannot be applied to a different tree by accident.
BASE_BOOTARGS = b"console=ttySAC2,115200 vmalloc=512M clk_ignore_unused\x00"
# Shorter than the base, which is what lets it fit without moving the tree.
# No baud rate: a gadget serial port has no physical line to set a rate on,
# and console=ttyGS0 with no options is the form Android uses for this very
# port. console=ttySAC2,115200 is 23 bytes and console=ttyGS0 is 15, so the
# corrected line is shorter and the property does not have to grow.
NEW_BOOTARGS = b"console=ttyGS0 vmalloc=512M clk_ignore_unused\x00"
# The rebuilt kernel: same source and same config as the r1 kernel above, with
# the single CONFIG_FRAMEBUFFER_CONSOLE=y change, built by pmbootstrap from
# linux-samsung-k3gxx-3.10.9-r2. Asserted so a stray or truncated kernel cannot
# be packed by accident.
NEW_KERNEL_SHA256 = "4a559bc8b309dcd959db2d2d7d30cf2d3cdfeae2bf41a6c8e0579fa6e59d7377"
BOOT_LIMIT = 13_631_488

TARGET = "init_functions.sh"
# Both are static ARM helpers the initramfs runs before the real root exists.
# s5screen is deliberately absent in r26: the overlay is out of the image.
# The path is still taken on the command line and still validated, so a
# change to the file cannot slip past unnoticed while it is being ignored.
NEW_FILES = ("usr/bin/s5iskey", "usr/bin/s5usbkeep")

# ---------------------------------------------------------------- r19 keeper
# The r19 delta is exactly two files in the base ramdisk, both of them the
# CACHE diagnostic logger. DIAG_DIR holds the replacements; the base copies are
# read back out of the base image and pinned by hash, so a drifted base cannot
# slip through unnoticed.
HERE = Path(__file__).resolve().parent
DIAG_DIR = HERE / "diagnostic-ramdisk-r26"
DIAG_INIT2ND = "init_2nd.sh"
DIAG_S5DIAG = "usr/bin/s5diag"

BASE_INIT2ND_SHA256 = "4e2f3a698118e2fbacecb9e9cac94045d121c2e4f50ede0b6a3afd685e4a5450"
BASE_S5DIAG_SHA256 = "7e71d1b87fa6b5549a5b635590714aabbd69cdfed68ceb1cf0b0532ecf1267d2"

# The r18 ramdisk. Built with --no-diag this repack must land on exactly these
# bytes, which is what makes "r19 = r18 + two diagnostic files" a checked
# statement rather than a claim.
R18_RAMDISK_SHA256 = "d2cdffe1d4922062b96dc7e8c91ad80bc30daf5e73b8f96b8ac012529269c7e8"

# The only two edits to init_2nd.sh, as (base, r19). Reversing them must
# reproduce the base file byte for byte, which is asserted in run().
INIT2ND_EDITS = (
    (
        '/sbin/s5diag stop "$S5_DIAG_PID" || true',
        '/sbin/s5diag handover "$S5_DIAG_PID" || true',
    ),
    (
        '\tif ! [ "$pid" = "1" ]; then',
        '\tif ! [ "$pid" = "1" ] && ! [ "$pid" = "$S5_DIAG_PID" ]; then',
    ),
)

# r20 adds one block to r19's init_2nd.sh, and removing it has to reproduce
# r19's file byte for byte. That is asserted in run(), against r19's own copy,
# so "r20 = r19 + one block" is a checked statement.
R19_INIT2ND_SHA256 = "0c0074afaea5eee559847da355a10ba663db2a134f8e729d8c1b9d936f58a969"
INSTALL_BLOCK_BEGIN = "# r20: install the USB keeper into the real system"
INSTALL_BLOCK_END = "# Switch root\nrun_hooks /hooks-cleanup"
# r22 replaces exactly this r21 branch: the console is no longer silenced.
R26_INIT2ND_SHA256 = "200e1b9766ee3197808439ae102e99ba16d2927475d1e11463337859dc3f4b0f"


R23_INIT2ND_SHA256 = "bfee1c7c3773cae48ec70381f8eb4654118b2507e9ca72557bd58cf26e0ae403"



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
    """One newc (SVR4 with CRC) entry, fully 4-byte aligned.

    Both the name and the data have to be padded. Padding the name is obvious and
    was already here. Padding the data is the one that matters and was missing:
    newc requires every record to start on a 4-byte boundary, so if the data
    length is not a multiple of 4 the *next* header lands unaligned and the
    whole archive after that point is unreadable.

    This was a real bug, not a theoretical one. r19 added two helper binaries
    whose sizes happened to be divisible by 4, so the omission was invisible.
    r20 added a 7265-byte shell script, and 7265 % 4 == 1, so the record after
    it -- the TRAILER the kernel's unpacker stops on -- was shifted by one byte.
    The result was a boot image that flashed and read back perfectly, verified
    fine by a tolerant cpio parser, and still could not boot, because the
    initramfs was truncated. New files now assert their own alignment, and the
    builder re-walks the finished archive with no tolerance at all.
    """
    name_nul = name.encode() + b"\0"
    namesize = len(name_nul)
    header_len = 110 + namesize
    name_pad = align(header_len) - header_len
    data_pad = align(len(data)) - len(data)
    fields = [
        ino, mode, 0, 0, 1, mtime, len(data),
        0, 0, 0, 0, namesize, 0,
    ]
    out = b"070701" + b"".join(f"{f:08x}".encode() for f in fields)
    assert len(out) == 110, len(out)
    out += name_nul + b"\0" * name_pad
    out += data + b"\0" * data_pad
    assert len(out) % 4 == 0, (
        f"cpio record for {name} is {len(out)} B, not 4-byte aligned"
    )
    return out


def cpio_walk_strict(cpio: bytes, label: str):
    """Walk a newc archive refusing anything unusual, and return the entry names.

    This exists because the archive that bricked the r20 boot passed a tolerant
    parser. A parser that skips bytes it does not recognise will happily walk
    past a corrupt record and report the entries it managed to read, so the
    check has to stop at the first thing that is not exactly right: wrong magic,
    a zero-length name, an entry that overruns the buffer, or an archive that
    never reaches its TRAILER.
    """
    names = []
    pos = 0
    while pos + 110 <= len(cpio):
        if cpio[pos : pos + 6] != b"070701":
            raise AssertionError(
                f"{label}: bad cpio magic at offset {pos}: {cpio[pos:pos+6]!r}"
            )
        h = cpio[pos : pos + 110]
        namesize = int(h[94:102], 16)
        fsize = int(h[54:62], 16)
        if namesize == 0:
            raise AssertionError(f"{label}: zero-length name at offset {pos}")
        name = cpio[pos + 110 : pos + 110 + namesize - 1]
        data = pos + 110 + namesize
        data += (-data) % 4
        if data + fsize > len(cpio):
            raise AssertionError(
                f"{label}: entry {name!r} overruns the archive "
                f"(needs {data + fsize} B, have {len(cpio)})"
            )
        if name == b"TRAILER!!!":
            return names
        names.append(name.decode())
        pos = data + fsize
        pos += (-pos) % 4
    raise AssertionError(
        f"{label}: archive ended without a TRAILER record "
        f"({len(names)} entries read)"
    )


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



def undone_ok(candidate, base):
    """True if reversing the r19 edits reproduces the base bytes exactly."""
    out = candidate
    for old, edit in INIT2ND_EDITS:
        if out.count(edit.encode()) != 1:
            return False
        out = out.replace(edit.encode(), old.encode())
    return out == base


def run(base_path, functions_path, kernel_path, helpers, output_path, diag=True):
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

    # r26: send the kernel console to the USB gadget instead of an empty UART.
    #
    # /chosen/bootargs is the only command line this device ever sees. The boot
    # image's own header cmdline is inert, because the bootloader passes just the
    # device tree, and the r14 kernel log showed the full 850-byte line arriving
    # with neither "quiet" nor "buildvariant=eng" in it. The tree asks for
    # console=ttySAC2, and ttySAC2 is an internal UART with nothing wired to it,
    # so every printk the kernel ever emits goes to a closed port. The real
    # system then says out loud what that costs:
    #
    #   [pmOS-rd] Disabling console output again (use 'pmos.debug-shell' ...)
    #
    # ttyGS0 is one character shorter than ttySAC2, so the corrected command line
    # is shorter than the one it replaces and fits inside the 54 bytes the
    # property already reserves, with the baud rate dropped because a gadget
    # serial port has no physical line to set one on. That is the whole reason
    # this is done here rather than by rebuilding the tree: an FDT property that
    # has to grow moves every byte after it, and this does not.
    #
    # A console named on the kernel command line is set up before any userspace
    # runs, so unlike a write to /sys/class/tty/console/active it cannot be
    # undone afterwards by the real system.
    sys.path.insert(0, str(HERE / "source"))
    import fdt as fdt_mod

    tree = bytearray(dt[DTBH_LEN:])
    assert dt[:4] == b"DTBH" and len(dt) > DTBH_LEN, (
        "expected Samsung's DTBH wrapper in front of the tree"
    )
    tree_base = bytes(tree)
    f0 = fdt_mod.Fdt(tree_base)
    ba_off, ba_len = f0.find("/chosen", "bootargs")
    old_ba = f0.get("/chosen", "bootargs")
    assert old_ba == BASE_BOOTARGS, (
        f"the base bootargs is not the one this patch was written against: {bytes(old_ba)!r}"
    )
    new_ba = NEW_BOOTARGS
    assert len(new_ba) <= len(old_ba), (
        "the corrected command line will not fit in the bytes the property "
        "already reserves, and growing it would move the whole tree"
    )
    assert new_ba.count(b"console=ttyGS0") == 1, "the patch must add exactly one console"
    assert b"ttySAC2" not in new_ba, "the dead UART is still named in the command line"
    # The two must agree on every argument except the console, or this is not the
    # patch that was reviewed. Compared argument by argument, because comparing
    # tails of two strings of different lengths compares the wrong things.
    old_args = old_ba.rstrip(b"\x00").split(b" ")
    new_args = new_ba.rstrip(b"\x00").split(b" ")
    assert old_args[0] == b"console=ttySAC2,115200", (
        f"the base console argument is not the one this patch replaces: {old_args[0]!r}"
    )
    assert new_args[0] == b"console=ttyGS0", (
        f"the new console argument is wrong: {new_args[0]!r}"
    )
    assert old_args[1:] == new_args[1:], (
        f"the corrected command line changes more than the console: "
        f"{old_args[1:]} -> {new_args[1:]}"
    )

    # The property stores the string and its terminator in reserved space, so the
    # value handed to the writer is the string without the NUL; the writer pads
    # the rest of the reservation back to NUL, which is what the kernel reads up
    # to the first one anyway.
    #
    # Fdt copies whatever it is handed into a bytearray of its own, so the edited
    # blob has to be taken back off the object afterwards. Writing through a
    # throwaway Fdt(tree) would edit a copy and the boot would come up on the
    # dead UART again with every check in this file still green.
    fw = fdt_mod.Fdt(tree)
    fw.set_string_in_place("/chosen", "bootargs", new_ba.rstrip(b"\x00"))
    tree = fw.blob
    assert isinstance(tree, bytearray)

    # Prove the edit is confined. Reparse, re-read, and diff the whole blob: a
    # patch that moved a single byte anywhere else would be a tree the bootloader
    # may not accept, and "probably fine" is not a thing to say about a device
    # tree.
    f1 = fdt_mod.Fdt(bytes(tree))
    assert f1.totalsize == f0.totalsize, "the tree changed size"
    assert len(tree) == len(tree_base), "the tree changed length"
    assert f1.get("/chosen", "bootargs").rstrip(b"\x00") == new_ba.rstrip(b"\x00"), (
        "the bootargs did not read back"
    )
    assert f1.find("/chosen", "bootargs")[1] == ba_len, "the property changed length"
    # Confinement as an exact positive statement rather than a bound on a diff.
    # The old form was "no differing byte may fall outside this range", and a
    # range is a thing a mutation can widen: the control that widened it built an
    # image with every other check green. This reconstructs the whole tree from
    # the base and the one intended edit and demands equality, so there is no
    # bound to relax and a stray byte anywhere fails.
    padded = new_ba.rstrip(b"\x00").ljust(ba_len, b"\x00")
    assert len(padded) == ba_len, "the new value does not fill the reservation exactly"
    expected = tree_base[:ba_off] + padded + tree_base[ba_off + ba_len:]
    assert tree == expected, (
        "the patched tree is not the base tree with only /chosen/bootargs "
        "replaced, so something else moved"
    )
    differing = {i for i, (a, c) in enumerate(zip(tree_base, tree)) if a != c}
    assert differing, "the bootargs patch changed nothing at all"
    ba_patch_len = len(differing)
    print(f"bootargs   {bytes(old_ba).rstrip(chr(0).encode())!r}")
    print(f"         ->{bytes(new_ba).rstrip(chr(0).encode())!r}"
          f"   {ba_patch_len} B changed, all inside /chosen/bootargs, tree unmoved")
    dt = bytes(dt[:DTBH_LEN]) + bytes(tree)

    # The one intended kernel change: swap in the rebuilt fbcon kernel. The
    # device tree is deliberately taken from the base, not from the new build,
    # so the DT cannot drift even though the new build also emitted one.
    kernel = kernel_path.read_bytes()
    assert hashlib.sha256(kernel).hexdigest() == NEW_KERNEL_SHA256, (
        "replacement kernel is not the expected fbcon rebuild"
    )

    replacement = functions_path.read_bytes()
    # r26: the framebuffer overlay is out of this image.
    #
    # s5status is replaced where it already is rather than appended after the
    # file, and that placement is the whole point. The strip-back check at the
    # end of this function drops every marked region and demands the remainder
    # equal the base byte for byte, so anything added outside a region fails
    # that check. Appending was tried and the check caught it, which is the
    # check doing its job: the added code would not have been part of anything
    # the rest of this builder reasons about.
    #
    # Fail-open, exactly as the base was: no output, exit 0. Every call site
    # keeps its shape and its result, so the six states the base painted are
    # still named in the file and still reported. What is removed is the
    # painting, not the reporting that it was painting.
    _old_status = (
        's5status() {\n'
        '\t[ "$nos5screen" = "y" ] && return 0\n'
        '\ts5screen "$1" >/dev/null 2>&1\n'
        '\treturn 0\n'
        '}\n'
    ).encode()
    _new_status = (
        's5status() {\n'
        '\t# r26: s5screen is not in this image. The overlay is removed and\n'
        '\t# the display is left to the console, which now reaches ttyGS0.\n'
        '\t# Nothing is painted, so there is no fb0 write here that can go\n'
        '\t# wrong, and the call sites below are unchanged.\n'
        '\treturn 0\n'
        '}\n'
    ).encode()
    assert replacement.count(_old_status) == 1, (
        "s5status is not the one the overlay removal was written against"
    )
    replacement = replacement.replace(_old_status, _new_status, 1)
    # The switch is gone with the thing it switched, and the variable that
    # only existed to hold it goes too rather than being left reading as if it
    # still controlled something.
    _old_nos = 'nos5screen="${nos5screen:-y}"\n'.encode()
    _new_nos = (
        '# r26: nos5screen is gone with s5screen. There is no overlay left to\n'
        '# switch off, and a variable left behind would read as if it still did.\n'
    ).encode()
    assert replacement.count(_old_nos) == 1, "the nos5screen default moved"
    replacement = replacement.replace(_old_nos, _new_nos, 1)
    blobs = {}
    for name, path in helpers.items():
        blob = path.read_bytes()
        if blob[:4] == b"\x7fELF":
            check_arm_static(name, blob)
        else:
            # Not everything the initramfs carries is a binary. s5usbkeep is a
            # /bin/sh script that the initramfs copies into the real system, so
            # what has to be checked is that it is a script and that it is one
            # the real system's shell can run. Asserting that here means a
            # mis-built or truncated file fails the build rather than failing
            # silently on the phone, which is the failure mode r19 had.
            assert blob.startswith(b"#!"), f"{name} is neither ELF nor a script"
            assert b"/bin/sh" in blob.splitlines()[0], (
                f"{name} has an interpreter the real system may not have: "
                f"{blob.splitlines()[0]!r}"
            )
            assert b"\r\n" not in blob, f"{name} has CRLF line endings"
        blobs[name] = blob

    raw = gzip.decompress(ramdisk)
    entries = list(parse(raw))
    names = [e[0] for e in entries]
    for nf in NEW_FILES:
        assert nf not in names, f"{nf} already exists in the base initramfs"
    assert names.count(TARGET) == 1, f"expected exactly one {TARGET} entry"
    base_target = next(e for e in entries if e[0] == TARGET)
    base_functions = raw[base_target[3] : base_target[3] + base_target[4]]

    # ------------------------------------------------------------------ r19
    # Pull the two diagnostic files out of the base and pin them, so the keeper
    # work is provably confined to files this script knows the original bytes of.
    base_diag = {}
    for target, want_sha in (
        (DIAG_INIT2ND, BASE_INIT2ND_SHA256),
        (DIAG_S5DIAG, BASE_S5DIAG_SHA256),
    ):
        assert names.count(target) == 1, f"expected exactly one {target} entry"
        entry = next(e for e in entries if e[0] == target)
        blob = raw[entry[3] : entry[3] + entry[4]]
        got = hashlib.sha256(blob).hexdigest()
        assert got == want_sha, f"base {target} changed: {got} != {want_sha}"
        base_diag[target] = blob

    replacements = {}
    if diag:
        new_init2nd = (DIAG_DIR / "init_2nd.sh").read_bytes()
        new_s5diag = (DIAG_DIR / "s5diag").read_bytes()

        # Step 1: rebuild r19's init_2nd.sh from the pinned base bytes, by
        # applying the same two edits r19 made. Each edit must be unique in both
        # directions and undoing them must land exactly back on the base, so the
        # change is reviewable as "these two lines" rather than as a new file.
        r19_init2nd = base_diag[DIAG_INIT2ND]
        for old, edit in INIT2ND_EDITS:
            assert r19_init2nd.count(old.encode()) == 1, (
                f"init_2nd.sh base edit is not unique: {old!r}"
            )
            r19_init2nd = r19_init2nd.replace(old.encode(), edit.encode())
        assert undone_ok(r19_init2nd, base_diag[DIAG_INIT2ND]), (
            "reversing the two init_2nd.sh edits does not reproduce the base file"
        )
        assert (
            hashlib.sha256(r19_init2nd).hexdigest() == R19_INIT2ND_SHA256
        ), "the rebuilt r19 init_2nd.sh does not match the shipped r19 file"

        # Step 2. r26's claims, checked against bytes and against the code.
        # Every r25 assertion is kept, because each one encodes a mistake that
        # actually shipped. The new ones cover the getty repair, the console and
        # the service matching, and they are anchored to whole lines.
        r26_init2nd = (HERE / "diagnostic-ramdisk-r26" / "init_2nd.sh").read_bytes()
        assert hashlib.sha256(r26_init2nd).hexdigest() == R26_INIT2ND_SHA256, (
            "the shipped r26 init_2nd.sh changed under this builder"
        )
        t = r26_init2nd.decode()
        lines = [re.sub(r"^\s*#.*$", "", ln) for ln in t.splitlines()]
        code = "\n".join(lines)
        assert "exec switch_root" in t, "init_2nd.sh no longer calls switch_root"
        switch = t.index("exec switch_root")

        def called(fn):
            n = "\n%s\n" % fn
            assert n in t, "%s is never called as a step in init_2nd.sh" % fn
            assert t.index(n) < switch, (
                "%s runs after switch_root, so the real system has already "
                "started and the step does nothing" % fn
            )
            return True

        def anyline(frag):
            return any(frag in ln for ln in lines)

        def exact(frag):
            """A whole line, stripped, equal to frag.

            Substring tests are how the gaps happened. A path or a command with
            a twin elsewhere in the file keeps the substring alive when the line
            that matters is changed, and four mutations in this project's own
            controls have built an image that did the wrong thing while every
            check passed.
            """
            return any(ln.strip() == frag for ln in lines)

        def count(frag):
            return sum(1 for ln in lines if frag in ln)

        checks = [
            # ---- the getty repair, which is what r26 is for ----
            # Anchored to the awk print line, not to the string. The same text
            # appears in the verification grep further down, so a substring test
            # stays green when the line that is actually written loses its baud
            # rate.
            ("the line actually written carries a speed getty can parse",
             exact('print "ttyGS0::respawn:/sbin/getty -L 115200 ttyGS0 vt100"')),
            ("the repair is verified against a whole line, not a substring",
             anyline("^ttyGS0::respawn:/sbin/getty -L 115200 ttyGS0 vt100$")),
            ("a repair that did not land is reported as a failure",
             anyline("WARNING the repair did not land")),
            ("the inittab is backed up before it is edited",
             anyline('cp -p "$it" "$bak"')),
            ("a failed rewrite restores the original",
             anyline('cp -f "$bak" "$it"')),
            ("only the first ttyGS0 getty line is rewritten",
             anyline("!done {") and anyline("done = 1")),
            ("the untouched backup is the source of the rewrite",
             anyline('"$bak" > "$it.s5new"')),
            ("the inittab before-state is logged, so the edit is not blind",
             anyline("getty lines in the real inittab, before:")),
            # ---- the console ----
            ("the console is pointed at ttyGS0", anyline('echo ttyGS0 > "$act"')),
            ("a console that will not turn on is reported, not ignored",
             anyline("WARNING could not point the console at ttyGS0")),
            ("and the previous console setting is logged either way",
             anyline("console/active was [")),
            # ---- matching services instead of naming them ----
            # r25 named s5-usb-ncm and the phone's screen showed a different
            # name in the boot runlevel. A name copied out of a listing is a
            # guess, and this boot cost a cycle to find that out.
            ("no USB service is named literally any more",
             "s5-usb-ncm" not in code and "s5-usb-ocn" not in code),
            ("candidates are matched by shape, case-folded",
             exact("low=$(echo \"$name\" | tr 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' "
                   "'abcdefghijklmnopqrstuvwxyz')")),
            ("and the shape list covers the names seen on the phone",
             all(anyline("*" + w + "*") for w in
                 ("usb", "ocn", "ncm", "acm", "gadget", "udc", "dwc3", "rndis"))),
            ("the list is computed once, so printing and stubbing cannot differ",
             exact("names=$(s5usb_usb_names)")),
            # Every loop over a service name must iterate that list. Expressed
            # as an equality rather than a count, because the number of loops
            # changes as more gets reported and a fixed count is a check that
            # goes stale rather than one that bites.
            ("and every loop over service names iterates it, never a literal",
             count("for name in") == count("for name in $names") >= 2),
            ("nothing matching is reported as a failure, not a quiet pass",
             anyline("WARNING no USB service matched")),
            # ---- the runlevel links, the r25 bug this photo caught ----
            # r25 tested with -e. These links point at /etc/init.d/..., which
            # does not exist in the initramfs, so -e was false for every one of
            # them: nothing was removed, and nothing said so either.
            ("runlevel links are tested with -L, which survives a dead target",
             anyline('[ -L "$link" ]')),
            ("and never with -e, which is what silently matched nothing",
             '[ -e "$link" ]' not in code),
            ("removing one is reported, one line per link",
             anyline("removed the runlevel link")),
            ("removing none is a warning, not silence",
             anyline("WARNING nothing was unlinked")),
            ("the stub is written whole, not appended to",
             exact("cat > \"$f\" <<'STUB'")),
            ("the original is kept before it is replaced",
             anyline('cp -p "$f" "$f.s5bak"')),
            ("and a failure to keep it stops that service being touched",
             anyline("could not back up $f, leaving it alone")),
            # ---- carried over from r25, still true ----
            ("CACHE is mounted by device, not assumed to be /cache",
             anyline("mount -t ext4") and "/mnt/s5r25" in code),
            ("the dump directory is under the device mount",
             exact("out=/mnt/s5r25/codex-s5-diagnostics/r26")),
            ("and nothing points the dump back at the /cache path",
             not any(ln.strip().startswith("out=/cache") for ln in lines)),
            ("no bind mount is attempted anywhere", "mount -o bind" not in code),
            ("nothing shadows a sysfs attribute", "/sys/class/android_usb" not in code),
            ("nothing claims USB is unlosable", "unlosable" not in code),
            ("the inittab append is guarded by a grep first",
             anyline("grep -q '^s5usbconsole::'")),
            ("a login-free shell is still offered",
             anyline("'s5usbconsole::respawn:/bin/sh -i'")),
            ("the initramfs console is still silenced the way upstream does",
             "exec >/dev/null 2>&1" in code),
            ("the console is not pointed at /dev/console, which wedged r22",
             "exec >/dev/console" not in code),
        ]
        bad = [name for name, ok in checks if not ok]
        for name, ok in checks:
            print(f"  {'OK  ' if ok else 'FAIL'} {name}")
        assert not bad, "r26 init_2nd.sh does not do what it claims: %s" % bad
        for step in ("s5usb_open_shell", "s5usb_fix_getty",
                     "s5usb_console_on", "s5usb_neutralise"):
            called(step)
            print(f"  OK   {step} is called, and before switch_root")
        assert (HERE / "diagnostic-ramdisk-r26" / "s5usbkeep").read_text().count(
            "CACHE_LOGDIR=codex-s5-diagnostics/r26") == 1, (
            "the keeper still logs to a stale CACHE directory"
        )
        print("  OK   the keeper's CACHE log directory is r26")

        # The keeper has to keep everything the base logger did, or the pre-root
        # half of the log regresses. Every base-only line must still be present.
        base_text = base_diag[DIAG_S5DIAG].decode()
        new_text = new_s5diag.decode()
        for line in base_text.splitlines():
            if line.strip() and line not in new_text:
                raise AssertionError(f"r19 s5diag dropped a base line: {line!r}")

        # init_2nd.sh must hand over, not stop. The reverse-substitution assert
        # above already pins its exact bytes; this states the intent so a future
        # edit that reintroduces the stop cannot pass quietly.
        assert b"s5diag handover" in new_init2nd
        assert b"s5diag stop" not in new_init2nd

        # And the keeper has to actually do the new job.
        assert "\nhandover)" in new_text, "r19 s5diag has no handover subcommand"
        assert "keep_acm" in new_text and "android_dir" in new_text
        assert "usb.log" in new_text

        # r20 ships a second script, s5usbkeep, which is a new file rather than
        # a replacement. It has to contain the whole job, because the one thing
        # this revision exists to fix is that a process living in the initramfs
        # cannot be relied on after switch_root.
        new_keep = (DIAG_DIR / "s5usbkeep").read_text()
        for needed in (
            "while :",                       # it must keep running for the boot
            'attr "$ad/functions"',          # and actually look at android_usb
            "assert_acm",                    # and be able to put acm back
            'echo "acm" > "$ad/functions"',  # with the proven write sequence
            'echo "0" > "$ad/enable"',      # disable first: functions_store
            'echo "1" > "$ad/enable"',      # is EBUSY while the device is on
            "save_pre_state",                # and leave evidence of the drift
            "dmesg",
            "/dev/mmcblk0p19",               # CACHE: the only channel TWRP can read
            "LOG_MAX",                       # a logger that fills CACHE loses its
        ):
            if needed not in new_keep:
                raise AssertionError(f"r20 s5usbkeep is missing {needed!r}")
        # A bare "watchdog" would collide with the fatal-signature keyword in the
        # log puller, the same reason the r19 tag avoided it.
        assert "watchdog" not in new_keep
        # It must not be a script that can exit quietly: every failure path in it
        # is either a note plus continue, or a loop, never a bare return at top.
        assert "exit 0" not in new_keep.split("while :")[0].split("setup_cache()")[-1]
        # A bare "watchdog" in the keeper's own tag would collide with the
        # fatal-signature keyword in the log puller and add noise to every pull.
        assert "TAG=s5keep" in new_text

        replacements = {DIAG_INIT2ND: new_init2nd, DIAG_S5DIAG: new_s5diag}
    else:
        replacements = {}

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
    diag_replaced = 0
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
        elif name in replacements:
            # The r19 keeper. Same in-place rewrite as TARGET, and the mode is
            # left exactly as the base had it so the file keeps its exec bit.
            blob = replacements[name]
            new_header = bytearray(header)
            new_header[54:62] = f"{len(blob):08x}".encode()
            out.extend(new_header)
            out.extend(raw[pos + 110 : data_start])
            out.extend(blob)
            out.extend(b"\0" * (align(len(blob)) - len(blob)))
            diag_replaced += 1
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
    assert diag_replaced == len(replacements), (
        f"replaced {diag_replaced} diagnostic files, expected {len(replacements)}"
    )
    cpio = bytes(out)

    # The archive has to be walked strictly before it is gzipped, not after the
    # image is built. The r20 boot image that could not boot was correct by every
    # other measure available: it hashed correctly, it flashed, it read back
    # byte for byte, and a tolerant cpio parser listed its files. The only thing
    # that noticed was a strict walk of the archive finding no TRAILER. So this
    # assert is the last line of defence and it runs on the bytes themselves.
    walked = cpio_walk_strict(cpio, "new ramdisk")
    assert walked[-1] != "TRAILER!!!", "the walker should not list the trailer"
    for nf in NEW_FILES:
        assert walked.count(nf) == 1, f"{nf} appears {walked.count(nf)} times"
    assert walked.count("init_2nd.sh") == 1, "init_2nd.sh is not unique"
    base_names = [e[0] for e in entries if e[0] != "TRAILER!!!"]
    assert walked[-1] != "TRAILER!!!"
    missing = [n for n in base_names if n not in walked]
    assert not missing, f"the rebuilt archive lost {missing}"

    stream = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=stream, compresslevel=9, mtime=0) as gz:
        gz.write(cpio)
    new_ramdisk = stream.getvalue()

    # With the keeper switched off this repack must land on r18's ramdisk byte
    # for byte. That is what pins the r19 delta to the two diagnostic files and
    # nothing else -- in particular it rules out the gzip level, the mtime or
    # the cpio layout having drifted underneath an unchanged init_functions.sh.
    if not diag:
        got = hashlib.sha256(new_ramdisk).hexdigest()
        assert got == R18_RAMDISK_SHA256, (
            f"--no-diag ramdisk is not r18's: {got} != {R18_RAMDISK_SHA256}"
        )
        print(f"no-diag    ramdisk matches r18 exactly ({got[:16]})")
    else:
        for target, blob in sorted(replacements.items()):
            print(f"keeper     {target} {len(blob)} B  {hashlib.sha256(blob).hexdigest()[:16]}")

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
    print(f"device tree{hashlib.sha256(dt).hexdigest()}  (bootargs patched)")

    # End to end, and independently of every line above: take the device tree
    # back out of the image that was just written, using only the header in those
    # written bytes, and read the command line the bootloader will actually hand
    # the kernel. This is checked against the finished image rather than against
    # the patcher's opinion of its own work, and it is the one check here that
    # survives every other check in this file being deleted.
    _h = struct.unpack_from("<10I", written, 8)
    _p = _h[7]
    assert _p == page_size, "the written page size changed"
    _ko = _p
    _ro = _ko + align(_h[0], _p)
    _do = _ro + align(_h[2], _p)
    written_dt = written[_do:_do + _h[8]]
    assert len(written_dt) == _h[8], "the written device tree is truncated"
    assert written_dt[:4] == b"DTBH", "the written device tree lost its wrapper"
    wt = fdt_mod.Fdt(bytearray(written_dt[DTBH_LEN:]))
    shipped = wt.get("/chosen", "bootargs")
    assert shipped.rstrip(b"\x00") == new_ba.rstrip(b"\x00"), (
        f"the written image does not carry the corrected command line: "
        f"{bytes(shipped)!r}"
    )
    assert shipped.startswith(b"console=ttyGS0"), "the shipped console is not ttyGS0"
    assert b"ttySAC2" not in shipped, (
        "the written command line still names the dead UART"
    )
    print(f"           re-read from the written image: "
          f"{bytes(shipped).rstrip(chr(0).encode())!r}")

    print(f"cmdline    {written_cmdline.decode()!r}")
    print(f"{TARGET} {len(base_functions)} -> {len(replacement)} B, "
          f"all {len(REGIONS)} regions strip back to the base")
    for nf in NEW_FILES:
        kind = (
            "stripped static ARM" if blobs[nf][:4] == b"\x7fELF" else "shell script"
        )
        print(f"{nf} {len(blobs[nf])} B, {kind}, added")
    print(f"headroom   {BOOT_LIMIT - len(image)} B of {BOOT_LIMIT}")


if __name__ == "__main__":
    if len(sys.argv) not in (8, 9):
        raise SystemExit(
            "usage: repack-boot-r20.py base-boot.img init_functions.sh "
            "vmlinuz s5screen s5iskey s5usbkeep output.img [--no-diag]"
        )
    diag = "--no-diag" not in sys.argv[8:]
    args = [a for a in sys.argv[1:] if a != "--no-diag"]
    (
        base_path,
        functions_path,
        kernel_path,
        s5screen_path,
        s5iskey_path,
        s5usbkeep_path,
        output_path,
    ) = (Path(x) for x in args)
    run(
        base_path,
        functions_path,
        kernel_path,
        {
            "usr/bin/s5iskey": s5iskey_path,
            "usr/bin/s5usbkeep": s5usbkeep_path,
        },
        output_path,
        diag=diag,
    )
