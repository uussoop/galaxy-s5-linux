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
# The rebuilt kernel: same source and same config as the r1 kernel above, with
# the single CONFIG_FRAMEBUFFER_CONSOLE=y change, built by pmbootstrap from
# linux-samsung-k3gxx-3.10.9-r2. Asserted so a stray or truncated kernel cannot
# be packed by accident.
NEW_KERNEL_SHA256 = "4a559bc8b309dcd959db2d2d7d30cf2d3cdfeae2bf41a6c8e0579fa6e59d7377"
BOOT_LIMIT = 13_631_488

TARGET = "init_functions.sh"
# Both are static ARM helpers the initramfs runs before the real root exists.
NEW_FILES = ("usr/bin/s5screen", "usr/bin/s5iskey", "usr/bin/s5usbkeep")

# ---------------------------------------------------------------- r19 keeper
# The r19 delta is exactly two files in the base ramdisk, both of them the
# CACHE diagnostic logger. DIAG_DIR holds the replacements; the base copies are
# read back out of the base image and pinned by hash, so a drifted base cannot
# slip through unnoticed.
HERE = Path(__file__).resolve().parent
DIAG_DIR = HERE / "diagnostic-ramdisk-r22"
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
CONSOLE_OLD = 'if [ -e "/proc/1/fd/3" ]; then\n\texec 1>&3 2>&4\nelif [ "$debug_shell" != "y" ]; then\n\techo "$LOG_PREFIX Disabling console output again (use \'pmos.debug-shell\' to keep it enabled)"\n\texec >/dev/null 2>&1\nfi\n\n'
CONSOLE_NEW = '# r22: do not silence the console. It used to be disabled here so the display\n# would show only the framebuffer overlay, and that was the right trade while\n# the overlay was the only thing drawing. The overlay is off now, so silencing\n# here left a black screen for the whole life of the boot: the display and the\n# USB console went quiet at exactly the moment the real system started, which\n# is the only part anyone wants to read. Keep it on. pmos.debug-shell is now\n# the default, not the exception.\nif [ -e "/proc/1/fd/3" ]; then\n\texec 1>&3 2>&4\nelse\n\techo "$LOG_PREFIX Keeping console output enabled (r22)"\n\texec >/dev/console 2>&1\nfi\n\n'
# r22 inserts this verbatim; splicing it out is a plain delete.
SHADOW_BLOCK = '# r22: make android_usb impossible to reconfigure.\n#\n# Four boots in a row now prove the same thing from both ends. The initramfs\n# binds acm and it holds for 2 min 53 s, then the real system takes it and never\n# gives it back. r20 and r21 each installed a keeper in the real system, inittab\n# line verified present and exact on disk, and neither keeper ever wrote a line.\n# And the initramfs-side keeper, whose handover flag provably landed on CACHE at\n# 06:45:25, was gone within five seconds of that flag appearing. Keeping a\n# function bound by fighting for it has failed on both sides of switch_root, and\n# the reason is structural: busybox switch_root tears down the old root, so\n# anything on the old side of the barrier has no /run and no /dev to work with,\n# and anything in the real system depends on an init mechanism that has not\n# respawned it.\n#\n# So do not fight. /sys is mounted by the initramfs and the real system does not\n# remount it, which means a bind mount made here is still in place after\n# switch_root. Bind mounting ordinary files over the two writable attributes\n# means every write the real system makes to them lands in a file in this\n# initramfs\'s memory instead of reaching the driver. "echo ncm > functions"\n# succeeds, the real system believes it configured USB, android_usb stays bound\n# to acm, and the serial console never goes away. There is no keeper to install\n# and nothing to uninstall afterwards.\n#\n# The cost is stated plainly: the real system\'s USB configuration will not take\n# effect, so no ncm networking and no adb. That is the trade being made on\n# purpose -- an interactive console is worth more here than USB networking, and\n# networking can be set up from inside once a shell exists.\ns5usb_shadow() {\n\tad=""\n\tfor c in /sys/class/android_usb/android*; do\n\t\t[ -e "$c/functions" ] && {\n\t\t\tad="$c"\n\t\t\tbreak\n\t\t}\n\tdone\n\tif [ -z "$ad" ]; then\n\t\techo "INFO: no android_usb device to shadow, leaving USB alone"\n\t\treturn 0\n\tfi\n\techo "INFO: android_usb at $ad, functions=[$(cat "$ad/functions")] enable=[$(cat "$ad/enable")]"\n\n\tshadowed=0\n\tfor attr in functions enable; do\n\t\t[ -e "$ad/$attr" ] || continue\n\t\t# The decoy starts as a copy of what the driver already holds, so a\n\t\t# reader that echoes it back sees the truth.\n\t\tcat "$ad/$attr" > "/tmp/s5decoy_$attr" 2>/dev/null || : > "/tmp/s5decoy_$attr"\n\t\tchmod 0666 "/tmp/s5decoy_$attr" 2>/dev/null\n\t\tif mount -o bind "/tmp/s5decoy_$attr" "$ad/$attr" 2>/dev/null; then\n\t\t\tshadowed=$((shadowed + 1))\n\t\t\techo "INFO: shadowed $ad/$attr, real-system writes to it now land in this file"\n\t\telse\n\t\t\techo "INFO: WARNING could not bind over $ad/$attr, USB can still be taken"\n\t\tfi\n\tdone\n\t# The class-level attributes are the ones the real userland writes, but the\n\t# per-function enables are writable too and a script that only sets\n\t# functions/acm/enable would undo the shadowing. Cover them as well.\n\tfor d in "$ad"/*/; do\n\t\t[ -f "$d/enable" ] || continue\n\t\tcat "$d/enable" > /tmp/s5decoy_fenable 2>/dev/null || : > /tmp/s5decoy_fenable\n\t\tchmod 0666 /tmp/s5decoy_fenable 2>/dev/null\n\t\tmount -o bind /tmp/s5decoy_fenable "$d/enable" 2>/dev/null \\\n\t\t\t&& echo "INFO: shadowed $d/enable" \\\n\t\t\t|| echo "INFO: WARNING could not bind over $d/enable"\n\tdone\n\n\t# Read the attributes back the way a writer would. If these still show acm\n\t# then the binds took; if they show something else, the shadowing is a lie\n\t# and the boot should not be trusted to keep a console.\n\tverify="$(cat "$ad/functions" 2>/dev/null | tr -d \'\\r\\n\')"\n\tif [ "$verify" = "acm" ]; then\n\t\techo "INFO: verify: functions reads back as acm through the shadow"\n\telse\n\t\techo "INFO: WARNING functions reads back as \'$verify\', the shadow did not take"\n\tfi\n\techo "INFO: shadowed $shadowed class attributes; acm is now unlosable"\n}\ns5usb_shadow\n\n'
R21_INIT2ND_SHA256 = "f17a761c6866fb7fe0f41a141b842bb93c4cfe3182fedcd002a30d78c059c230"


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

        # Step 2. r22's statement about init_2nd.sh is "r21 with two changes",
        # not "r19 with three": r21 already rewrote r20's install block, to add
        # the newline repair and the read-back verification, so the old
        # three-way reverse substitution against r19 could not be true and was
        # not. Both changes are checked here as exact text substitutions, so
        # the claim is about bytes and not about offsets. If a block is reworded
        # the substitution stops matching and the build stops, which is the
        # whole point: the r20 image flashed, read back with a matching md5 and
        # passed a tolerant parser while being unable to boot. A check that
        # quietly stops matching is worse than no check.
        r21_init2nd = (HERE / "diagnostic-ramdisk-r21" / "init_2nd.sh").read_bytes()
        assert (
            hashlib.sha256(r21_init2nd).hexdigest() == R21_INIT2ND_SHA256
        ), "the shipped r21 init_2nd.sh changed under this builder"
        assert new_init2nd.count(CONSOLE_NEW.encode()) == 1, (
            "the r22 console block is not present exactly once, so it cannot be "
            "reverted; reword it in init_2nd.sh and in this constant together"
        )
        assert new_init2nd.count(SHADOW_BLOCK.encode()) == 1, (
            "the r22 USB shadow block is not present exactly once"
        )
        reverted = new_init2nd.replace(CONSOLE_NEW.encode(), CONSOLE_OLD.encode(), 1)
        reverted = reverted.replace(SHADOW_BLOCK.encode(), b"", 1)
        assert reverted == r21_init2nd, (
            "reverting the r22 changes does not reproduce r21's init_2nd.sh"
        )
        print(f"  delta   init_2nd.sh usb-shadow     {len(SHADOW_BLOCK)} B inserted")
        print(
            f"  delta   init_2nd.sh console-keep   "
            f"{len(CONSOLE_NEW) - len(CONSOLE_OLD):+d} B"
        )
        assert "mount -o bind" in SHADOW_BLOCK, "the shadow block binds nothing"
        assert '"$ad/functions"' in SHADOW_BLOCK and '"$ad/enable"' in SHADOW_BLOCK
        assert "\ns5usb_shadow\n" in SHADOW_BLOCK, "s5usb_shadow is never called"
        assert "exec >/dev/null 2>&1" not in CONSOLE_NEW, (
            "the r22 console branch still silences the console"
        )
        # The shadow has to be set up before switch_root. After it, the binds
        # would be over attributes nothing is going to write to.
        assert new_init2nd.index(SHADOW_BLOCK.encode()) < new_init2nd.index(
            b"exec switch_root"
        ), "the USB shadow is set up after switch_root, where it does nothing"
        # And the console must not be silenced anywhere else in the file, which
        # is the one regression that would produce a black screen and a dead
        # console at the same moment.
        without_old = new_init2nd.replace(CONSOLE_OLD.encode(), b"")
        assert b"exec >/dev/null 2>&1" not in without_old, (
            "somewhere else in init_2nd.sh still silences the console"
        )
        # The install block is inherited from r21 unchanged, so it is covered by
        # the revert above rather than by sentinels of its own. It still has to
        # be a definition and a call, not just a definition.
        beg = new_init2nd.find(INSTALL_BLOCK_BEGIN.encode())
        end = new_init2nd.find(INSTALL_BLOCK_END.encode())
        assert beg != -1 and end > beg, "the install block is malformed"
        assert new_init2nd.count(INSTALL_BLOCK_BEGIN.encode()) == 1
        inserted = new_init2nd[beg : end + len(INSTALL_BLOCK_END)]
        assert b"\ns5usbkeep_install\n" in inserted, (
            "the s5usbkeep_install function is defined but never called"
        )

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
    print(f"device tree{hashlib.sha256(dt).hexdigest()}  (unchanged)")
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
            "usr/bin/s5screen": s5screen_path,
            "usr/bin/s5iskey": s5iskey_path,
            "usr/bin/s5usbkeep": s5usbkeep_path,
        },
        output_path,
        diag=diag,
    )
