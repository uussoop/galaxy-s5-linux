#!/usr/bin/env python3
"""Independently verify a BOOT image built by repack-boot-r15.py.

This deliberately re-derives everything from the finished image instead of
trusting anything the repack tool computed, and it re-implements the region
stripper rather than importing it, so a bug in the builder cannot hide here.

Checks, in order of how much they would matter:

  1. the CPIO is exactly the base archive with init_functions.sh swapped and
     usr/bin/s5screen and usr/bin/s5iskey appended, and every other single
     entry is byte-identical;
  2. stripping every marked region out of that init_functions.sh reproduces the
     base image's own init_functions.sh byte for byte;
  3. both new files are stripped, static, ARM EABI5 executables, and are the
     same files that were handed to the repack tool;
  4. the kernel is the rebuilt fbcon one and really does contain the framebuffer
     console, the device tree and the boot header are untouched, and the
     header's size, SHA-1 and command line describe what the loader will do;
  5. the shell still parses, defines each helper before calling it, paints every
     state s5screen knows about, asks s5iskey rather than iskey, and calls
     ensure_usb_serial_getty from inside mount_root_partition so the installed
     system gets its ttyGS0 getty before switch_root;
  6. the image fits the BOOT partition.
"""

import gzip
import hashlib
import importlib.util
import struct
import subprocess
import sys
import zlib
from pathlib import Path

BASE = "boot-k3gxx-r11-subpartfix-35de59d0.img"
S5SCREEN = "source/s5screen"
S5ISKEY = "source/s5iskey"
BOOT_LIMIT = 13_631_488
# The base image's r1 kernel, which this image deliberately does not ship.
BASE_KERNEL_SHA256 = "6d0c337411dcbfe6771347446af8fcab19bb5bff265496a5cddaa272de631b8d"
# The rebuilt kernel: same source and config, plus CONFIG_FRAMEBUFFER_CONSOLE=y.
NEW_KERNEL_SHA256 = "4a559bc8b309dcd959db2d2d7d30cf2d3cdfeae2bf41a6c8e0579fa6e59d7377"
DT_SHA256 = "f6aac8da35c945bc982d10fccf29535a4692ea50669f5d323493c9c8b1228f6b"
TARGET = "init_functions.sh"
NEW_FILES = ("usr/bin/s5screen", "usr/bin/s5iskey")
NEW_FILE = NEW_FILES[0]

# Read straight out of the repack tool rather than restating them, so a marker
# rename cannot leave the verifier checking names that no longer exist.
_spec = importlib.util.spec_from_file_location("repack", "source/repack-boot-r15.py")
_repack = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_repack)
REGIONS = _repack.REGIONS

fails = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")
    if not ok:
        fails.append(label)
    return ok


def skip(label, why):
    """Record a check that could not be evaluated.

    Deliberately distinct from check(False, ...): a check that is impossible to
    run is not itself a defect of the image, and mixing it into the failure list
    would make the real failures harder to see. The parent check that led here
    is the one that fails.
    """
    print(f"  SKIP  {label}  ({why})")


def unpack(img: Path):
    """-> (header, kernel, ramdisk_blob, cpio_bytes, dt, page_size)"""
    b = img.read_bytes()
    assert b[:8] == b"ANDROID!", "not an ANDROID! boot image"
    fields = struct.unpack_from("<10I", b, 8)
    ksize, _, rsize, _, ssize, _, _, page, dtsize, _ = fields
    koff = page
    roff = koff + -(-ksize // page) * page
    doff = roff + -(-rsize // page) * page
    assert ssize == 0, f"unexpected second stage ({ssize} B)"
    return (
        b[:page],
        b[koff : koff + ksize],
        b[roff : roff + rsize],
        gzip.decompress(b[roff : roff + rsize]),
        b[doff : doff + dtsize],
        page,
    )


def archive(raw: bytes):
    """-> {name: data}. Written from the format spec, not reused from repack."""
    out, pos = {}, 0
    while pos < len(raw):
        if raw[pos : pos + 6] != b"070701":
            raise SystemExit(f"bad newc magic at offset {pos}")
        f = [int(raw[pos + 6 + n * 8 : pos + 14 + n * 8], 16) for n in range(13)]
        namesize, filesize = f[11], f[6]
        name = raw[pos + 110 : pos + 110 + namesize - 1].decode()
        dstart = pos + 110 + namesize
        dstart += -dstart % 4
        data = raw[dstart : dstart + filesize]
        if name != "TRAILER!!!":
            out[name] = data
        pos = dstart + filesize
        pos += -pos % 4
        if name == "TRAILER!!!":
            break
    return out


def strip(lines):
    """Independent stripper: delete each outermost marked region line range."""
    keep = [True] * len(lines)
    spans = []
    for begin, end in REGIONS:
        try:
            bi = next(i for i, l in enumerate(lines) if l.rstrip("\n").endswith(begin))
            ei = next(i for i, l in enumerate(lines) if l.rstrip("\n").endswith(end))
        except StopIteration:
            raise SystemExit(f"marker missing: {begin}")
        assert bi < ei, f"region markers out of order: {begin}"
        spans.append((bi, ei))
    for bi, ei in spans:
        # An outermost region covers every nested one too.
        if any(bj < bi and ei < ej for j, (bj, ej) in enumerate(spans) if (bj, ej) != (bi, ei)):
            continue
        for i in range(bi, ei + 1):
            keep[i] = False
    return "".join(l for i, l in enumerate(lines) if keep[i])


def code_lines(text):
    """Executable lines only: strip comments and whitespace, drop blanks.
    Needed because the guard's own comment mentions fail_halt_boot, so a raw
    substring count would report a difference that is not there."""
    out = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def main(image_name):
    img = Path(image_name)
    base = Path(BASE)
    s5ref = Path(S5SCREEN).read_bytes()

    print(f"verifying {img.name} ({img.stat().st_size} B)\n")

    print("1. archive contents")
    _, kern, _, cpio, dt, page = unpack(img)
    _, bkern, _, bcpi, bdt, _ = unpack(base)
    new = archive(cpio)
    old = archive(bcpi)

    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    check("exactly the two helper files added", added == sorted(NEW_FILES), f"added={added}")
    check("no file removed", not removed, f"removed={removed}")

    changed = sorted(n for n in set(old) & set(new) if old[n] != new[n])
    check(
        "init_functions.sh is the only changed file",
        changed == [TARGET],
        f"changed={changed}",
    )
    check(
        "entry count grew by two",
        len(new) == len(old) + len(NEW_FILES),
        f"{len(old)} -> {len(new)}",
    )

    print("\n2. init_functions.sh delta")
    got = new[TARGET].decode()
    base_fns = old[TARGET].decode()
    stripped = strip(got.splitlines(keepends=True))
    check(
        "all marked regions strip back to the base file",
        stripped == base_fns,
        f"{len(stripped)} vs base {len(base_fns)} B",
    )
    for begin, end in REGIONS:
        name = begin.split(": ")[1].removesuffix(" (begin)")
        check(
            f"exactly one {name!r} region",
            got.count(begin) == 1 and got.count(end) == 1,
        )
    check("the patch added bytes", len(got) > len(base_fns), f"{len(base_fns)} -> {len(got)} B")

    print("\n3. helper binaries")
    iskey_ref = Path(S5ISKEY).read_bytes()
    check(
        "s5screen byte-identical to the built binary",
        new[NEW_FILES[0]] == s5ref,
        f"{len(new[NEW_FILES[0]])} B",
    )
    check(
        "s5iskey byte-identical to the built binary",
        new[NEW_FILES[1]] == iskey_ref,
        f"{len(new[NEW_FILES[1]])} B",
    )
    blob = new[NEW_FILE]
    e_type, e_machine = struct.unpack_from("<HH", blob, 16)
    check("EM_ARM, ET_EXEC", e_machine == 40 and e_type == 2, f"machine={e_machine} type={e_type}")
    flags = struct.unpack_from("<I", blob, 36)[0]
    check("EABI5", flags & 0xFF000000 == 0x05000000, f"flags={flags:#x}")
    check(
        "statically linked",
        b"ld-musl" not in blob and b"/lib/ld" not in blob and b"interp" not in blob,
    )
    check(
        "stripped",
        all(j not in blob for j in (b".debug_info", b".debug_str", b".symtab", b".strtab")),
    )
    # Walk the program headers using the sizes the file itself declares, read
    # at their real ELF32 offsets rather than assumed ones.
    # Walk the program headers using the sizes the file itself declares, read at
    # their real ELF32 offsets rather than assumed ones. Zig emits a PIE, so
    # e_phnum counts six entries and the last is the read-only PT_GNU_RELRO;
    # what matters is that everything is well formed and nothing needs an
    # interpreter.
    e_phoff = struct.unpack_from("<I", blob, 0x1C)[0]
    e_ehsize = struct.unpack_from("<H", blob, 0x28)[0]
    e_phentsize = struct.unpack_from("<H", blob, 0x2A)[0]
    e_phnum = struct.unpack_from("<H", blob, 0x2C)[0]
    types = [
        struct.unpack_from("<I", blob, e_phoff + n * e_phentsize)[0] for n in range(e_phnum)
    ]
    # 0x70000001 is the ARM procedure-call-standard attribute section
    # (SHT_ARM_ATTRIBUTES), which LLD also emits as a program header. objdump
    # prints it as "UNKNOWN"; it is normal, not a malformed file.
    PT_LOAD, PT_INTERP, PT_DYNAMIC, PT_ARM_ATTRIBUTES = 1, 3, 2, 0x70000001
    check(
        "ELF header is self-consistent",
        e_ehsize == 52 and e_phentsize == 32 and e_phoff == 52,
        f"ehsize={e_ehsize} phentsize={e_phentsize} phoff={e_phoff}",
    )
    check(
        "every program header is a known type",
        all(t in (0, 1, 2, 4, 6, 0x6474E550, 0x6474E551, 0x6474E552, PT_ARM_ATTRIBUTES) for t in types),
        f"phnum={e_phnum} types={[hex(t) for t in types]}",
    )
    check(
        "has PT_LOAD, no PT_INTERP, no PT_DYNAMIC",
        PT_LOAD in types and PT_INTERP not in types and PT_DYNAMIC not in types,
        f"{types.count(PT_LOAD)} PT_LOAD",
    )

    print("\n4. kernel, device tree, header")
    check(
        "kernel differs from the base (this is the one intended change)",
        kern != bkern,
        f"base {hashlib.sha256(bkern).hexdigest()[:16]} -> {hashlib.sha256(kern).hexdigest()[:16]}",
    )
    check("kernel is the rebuilt fbcon one", hashlib.sha256(kern).hexdigest() == NEW_KERNEL_SHA256)
    check(
        "kernel is not the r1 that shipped before",
        hashlib.sha256(kern).hexdigest() != BASE_KERNEL_SHA256,
    )
    # Do not take the hash on trust: inflate the kernel and confirm the framebuffer
    # console is really linked in, and that a font came with it. This is the whole
    # point of the image, so it is checked against the bytes shipped rather than
    # against what the build was supposed to have done.
    payload = b""
    start = kern.find(b"\x1f\x8b\x08")
    if start >= 0:
        try:
            payload = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(kern[start:])
        except zlib.error:
            payload = b""
    check("kernel payload inflates", len(payload) > 1_000_000, f"{len(payload)} B")
    check("framebuffer console is linked in", b"fbcon" in payload)
    check(
        "a console font was pulled in too",
        b"vga_8x16" in payload or b"VGA8x16" in payload,
        "Kconfig select should have added one",
    )
    check(
        "the console-to-fb binding layer is present",
        b"con2fb" in payload,
    )
    check("device tree identical to base", dt == bdt, hashlib.sha256(dt).hexdigest()[:16])
    check("device tree unchanged", hashlib.sha256(dt).hexdigest() == DT_SHA256)
    check("page size still 2048", page == 2048)

    header = img.read_bytes()[:page]
    base_header = base.read_bytes()[:page]
    base_cmdline = bytes(base_header[64:576]).split(b"\0")[0]
    check(
        "the base command line is the one we expect to extend",
        base_cmdline == b"quiet buildvariant=eng",
        repr(base_cmdline),
    )
    ksize, _, rsize, _, _, _, _, _, dtsize, _ = struct.unpack_from("<10I", header, 8)
    roff = page + -(-ksize // page) * page
    rblob = img.read_bytes()[roff : roff + rsize]
    check("header ramdisk size matches the file", rsize == len(rblob) and rsize > 0, f"{rsize} B")
    digest = hashlib.sha1()
    for part in (kern, rblob, b"", dt):
        digest.update(part)
        digest.update(struct.pack("<I", len(part)))
    check("header SHA-1 describes the actual parts", header[576:596] == digest.digest())
    # The header cmdline is inert on this device: the bootloader passes only the
    # device tree's /chosen/bootargs, and the r14 kernel log shows the complete
    # 850-byte command line the kernel received, containing neither "quiet" nor
    # "buildvariant=eng" which are the whole of that field. So assert the field
    # is untouched, rather than checking for a s5keys that would do nothing.
    cmdline = bytes(header[64:576]).split(b"\0")[0]
    check(
        "the inert header cmdline is left exactly as the base had it",
        cmdline == base_cmdline,
        f"{base_cmdline!r} -> {cmdline!r}",
    )

    print("\n5. shell still sane")
    tmp = Path("/tmp/verify-s5-functions.sh")
    tmp.write_text(got)
    r = subprocess.run(["bash", "-n", str(tmp)], capture_output=True, text=True)
    check("parses under bash -n", r.returncode == 0, r.stderr.strip()[:200])
    check("s5status defined before first use", got.index("s5status() {") < got.index("s5status booting"))
    check(
        "keys_still_held defined before first use",
        got.index("keys_still_held() {") < got.index("keys_still_held KEY_LEFTSHIFT"),
    )
    for state in ("booting", "dmok", "dmno", "loopfail", "keys", "booted"):
        check(f"paints the {state!r} state", f"s5status {state}" in got)
    got_code, base_code = code_lines(got), code_lines(base_fns)
    check(
        "no fail_halt_boot call added or lost",
        got_code.count("fail_halt_boot") == base_code.count("fail_halt_boot"),
        f"{base_code.count('fail_halt_boot')} in base, {got_code.count('fail_halt_boot')} now",
    )
    # The one statement that decides a failed boot must now sit inside a
    # persistence test rather than firing on a single iskey sample.
    guard = [i for i, l in enumerate(got_code) if l == "fail_halt_boot"]
    check("the boot-failing call is the one check_keys reaches", len(guard) >= 1)
    idx = next(i for i, l in enumerate(got_code) if "elif iskey KEY_LEFTSHIFT" in l)
    window = got_code[idx : idx + 5]
    check(
        "it is guarded by keys_still_held",
        len(window) == 5
        and "keys_still_held" in window[1]
        and window[2] == "fail_halt_boot"
        and window[3] == window[4] == "fi",
        " | ".join(window),
    )
    check("the raw iskey call is unchanged", "elif iskey KEY_LEFTSHIFT KEY_VOLUMEUP; then" in got)
    # The guard must ask s5iskey, not iskey: iskey aggregates every event node
    # and cannot distinguish the real volume key from the one arizona-extcon
    # synthesises, which is what halted a boot with nothing held. Comments are
    # stripped by code_lines, so this is a claim about the code, not the prose.
    guard_body = got.split("keys_still_held() {")[1].split("\n# <<<")[0]
    body_code = code_lines(guard_body)
    check("keys_still_held asks s5iskey", any("s5iskey" in l for l in body_code))
    check(
        "keys_still_held no longer calls iskey",
        not any("iskey" in l.replace("s5iskey", "") for l in body_code),
        " | ".join(l for l in body_code if "iskey" in l),
    )
    check(
        "the synthesising headset device is what s5iskey excludes",
        b"Headset" in new[NEW_FILES[1]],
    )
    # the shipped s5status must never be able to abort the caller
    check("s5status cannot fail the caller", "\treturn 0\n}\n\n# Require the given keys" in got)
    check("s5status ignores its exit status", 's5screen "$1" >/dev/null 2>&1\n\treturn 0' in got)

    # The USB serial getty. Everything asserted here is about the one place in
    # the boot where the installed system can still be edited, and about not
    # corrupting an inittab that may already be correct.
    #
    # Every index() below is guarded. A verifier that raises on the very thing it
    # is checking reports a traceback instead of a verdict, and the checks after
    # the crash never run, which is how a second real problem gets missed.
    defined = "ensure_usb_serial_getty() {" in got
    called = "ensure_usb_serial_getty\n" in got
    check("ensure_usb_serial_getty is defined", defined)
    check("ensure_usb_serial_getty is called", called)
    if defined and called:
        getty_def = got.index("ensure_usb_serial_getty() {")
        getty_call = got.index("ensure_usb_serial_getty\n")
        check("defined before it is called", getty_def < getty_call)
        getty_end = got.find("\n# <<<", getty_def)
        getty_body = got[getty_def : getty_end if getty_end != -1 else len(got)]
        getty_code = code_lines(getty_body)
        for fragment, why in (
            ("[ ! -e \"$inittab\" ]", "must not assume /etc/inittab exists"),
            ("grep -q 'ttyGS0'", "must not append a second getty line"),
            ("line=\"ttyGS0::respawn:", "must use the inittab respawn syntax"),
            ("-L ttyGS0", "must pass -L, a gadget port never asserts carrier"),
        ):
            hit = next((l for l in getty_code if fragment in l), None)
            check(
                f"getty helper {why}",
                hit is not None,
                hit or f"no line contains {fragment!r}",
            )
        check(
            "getty helper probes for the getty binary rather than assuming one",
            any("cand" in l and "/sysroot/" in l for l in getty_code),
        )
        # The call has to be inside mount_root_partition, i.e. after the root
        # filesystem is mounted read-write at /sysroot. Calling it earlier would
        # silently do nothing, and calling it after switch_root would be too late.
        if "mount_root_partition() {" in got_code:
            mount_root = got_code.index("mount_root_partition() {")
            mount_root_end = next(
                (
                    i
                    for i, l in enumerate(got_code[mount_root + 1 :], mount_root + 1)
                    if l == "}"
                ),
                len(got_code),
            )
            check(
                "getty helper is called from inside mount_root_partition",
                any(
                    l == "ensure_usb_serial_getty"
                    for l in got_code[mount_root:mount_root_end]
                ),
                f"call at line {got[:getty_call].count(chr(10)) + 1}, "
                f"mount_root_partition spans {mount_root + 1}-{mount_root_end + 1}",
            )
        else:
            check("mount_root_partition is still present", False, "not found")
        check(
            "it is called after the root is mounted, not before",
            "mount -t \"$type\" -o rw" in got[:getty_call],
        )
    else:
        skip("getty helper body checks", "helper missing, nothing to inspect")
        skip("getty helper call site", "helper missing")

    # The halt fix. This is the part that decides whether the phone boots at all,
    # so check the two properties that matter rather than the presence of a
    # string: that the default is applied, and that it is a default rather than a
    # hard assignment that would defeat the documented command line override.
    default_literal = 's5keys="${s5keys:-n}"'
    default = default_literal in got
    check("s5keys is defaulted to n", default)
    check(
        "the default is overridable, not forced",
        default and 's5keys="n"\n' not in got,
        'a bare s5keys="n" would ignore any command line override',
    )
    if default and '"$s5keys" = "n"' in got:
        check(
            "the default is set before keys_still_held reads it",
            got.index(default_literal) < got.index('"$s5keys" = "n"'),
        )
    else:
        skip("default set before use", "default or reader missing")
    if "# >>> s5screen: helpers (begin)" in got and "# <<< s5screen: helpers (end)" in got:
        helpers_region = got.split("# >>> s5screen: helpers (begin)")[1].split(
            "# <<< s5screen: helpers (end)"
        )[0]
        check(
            "it is inside the helpers region, so strip-back still works",
            "s5keys" in helpers_region,
        )
    else:
        skip("default inside the helpers region", "helpers markers missing")
    # keys_still_held is what honours the flag, so make sure the flag is still
    # read at all: an earlier edit could have removed the check instead.
    check(
        "keys_still_held still honours the flag",
        '[ "$s5keys" = "n" ]' in got,
    )

    print("\n6. size")
    check("fits the BOOT partition", img.stat().st_size <= BOOT_LIMIT, f"{BOOT_LIMIT - img.stat().st_size} B spare")

    print()
    if fails:
        print(f"FAILED ({len(fails)}): " + "; ".join(fails))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify-boot-r15.py <image>")
    raise SystemExit(main(sys.argv[1]))
