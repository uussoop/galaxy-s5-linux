#!/usr/bin/env python3
"""Derive the r26 boot-image builder from the r25 one.

Every r25 assertion is kept, because each one encodes a mistake that actually
shipped, and two of them are re-anchored because r26 changes the code they
watch. The new work is threefold.

The getty repair, which is what r26 is for. The real inittab says

    ttyGS0::respawn:/sbin/getty -L ttyGS0 vt100

and busybox getty reads vt100 as a baud rate, dies, and is respawned forever, so
the console produces "getty: bad speed: vt100" and nothing else.

The device tree's /chosen/bootargs, which already asks for console=ttySAC2. That
is an internal UART with nothing wired to it, so all of the kernel's printk goes
nowhere and the real system then says it is turning the console off on purpose.
ttySAC2 and ttyGS0 are the same length, so the swap fits in the 54 bytes the
property already reserves and the tree does not move. The patch is confined,
asserted byte for byte, and reversible.

The service matching, which stops r26 repeating r25's mistake. r25 named one
service from a listing and the phone's screen showed a different one, and r25's
runlevel removal silently matched nothing because it tested symlinks with -e
against targets that do not exist in the initramfs.

The checks are anchored to whole lines. That is not pedantry: a substring test
cannot tell the line that matters from a twin of it elsewhere in the file, and
four mutations in this project's own controls have now built an image that did
the wrong thing while every check passed.
"""
import hashlib
import pathlib
import re

b = pathlib.Path("source/repack-boot-r25.py")
s = b.read_text()


def sub1(old, new):
    global s
    assert s.count(old) == 1, "no unique match: %r" % old[:80]
    s = s.replace(old, new, 1)


sub1('DIAG_DIR = HERE / "diagnostic-ramdisk-r25"', 'DIAG_DIR = HERE / "diagnostic-ramdisk-r26"')
sub1('HERE / "diagnostic-ramdisk-r25" / "init_2nd.sh"', 'HERE / "diagnostic-ramdisk-r26" / "init_2nd.sh"')
sub1('HERE / "diagnostic-ramdisk-r25" / "s5usbkeep"', 'HERE / "diagnostic-ramdisk-r26" / "s5usbkeep"')
s = re.sub(r"R25_INIT2ND_SHA256", "R26_INIT2ND_SHA256", s)
lines = s.splitlines(keepends=True)
pins = [i for i, ln in enumerate(lines) if ln.startswith("R26_INIT2ND_SHA256 = ")]
assert len(pins) == 1, "expected exactly one hash pin, found %d" % len(pins)
lines[pins[0]] = 'R26_INIT2ND_SHA256 = "%s"\n' % hashlib.sha256(
    pathlib.Path("source/diagnostic-ramdisk-r26/init_2nd.sh").read_bytes()).hexdigest()
s = "".join(lines)

# The device tree patch, applied where the tree is taken out of the base.
DT_PATCH = r'''    assert hashlib.sha256(dt).hexdigest() == DT_SHA256

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
'''

sub1("    assert hashlib.sha256(dt).hexdigest() == DT_SHA256\n", DT_PATCH)
DT_VERIFY = r'''
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
'''

# ---------------------------------------------------------------------------
# r26: take the framebuffer overlay out of the image entirely.
#
# s5screen paints state over /dev/fb0. It was there because there was nothing
# else to see: no console, no network, and a display that needed to say
# something. That is no longer true. The console now survives switch_root, the
# kernel sends printk to ttyGS0, and the real system gives a login prompt, so the
# overlay is now the only thing standing between the screen and a readable
# terminal, and it paints over the one prompt the display actually has.
#
# It was already inert at runtime, nos5screen defaulted to y and s5status
# returned before doing anything, so this changes no behaviour. It removes 7176 B
# of code that can only hurt: a failure inside it costs the screen, never a
# boot, which is still not something worth carrying once it has no job.
#
# Done by redefining s5status after the base bytes are read, rather than by
# editing the pinned init_functions source, so the region markers that the
# strip-back-to-base checks depend on are untouched. The call sites stay, and
# the checks that every state is still painted stay, because what is being
# removed is the painting and not the reporting that it was painting.
# ---------------------------------------------------------------------------
sub1("    replacement = functions_path.read_bytes()\n",
     "    replacement = functions_path.read_bytes()\n"
     "    # r26: the framebuffer overlay is out of this image.\n"
     "    #\n"
     "    # s5status is replaced where it already is rather than appended after the\n"
     "    # file, and that placement is the whole point. The strip-back check at the\n"
     "    # end of this function drops every marked region and demands the remainder\n"
     "    # equal the base byte for byte, so anything added outside a region fails\n"
     "    # that check. Appending was tried and the check caught it, which is the\n"
     "    # check doing its job: the added code would not have been part of anything\n"
     "    # the rest of this builder reasons about.\n"
     "    #\n"
     "    # Fail-open, exactly as the base was: no output, exit 0. Every call site\n"
     "    # keeps its shape and its result, so the six states the base painted are\n"
     "    # still named in the file and still reported. What is removed is the\n"
     "    # painting, not the reporting that it was painting.\n"
     "    _old_status = (\n"
     "        's5status() {\\n'\n"
     "        '\\t[ \"$nos5screen\" = \"y\" ] && return 0\\n'\n"
     "        '\\ts5screen \"$1\" >/dev/null 2>&1\\n'\n"
     "        '\\treturn 0\\n'\n"
     "        '}\\n'\n"
     "    ).encode()\n"
     "    _new_status = (\n"
     "        's5status() {\\n'\n"
     "        '\\t# r26: s5screen is not in this image. The overlay is removed and\\n'\n"
     "        '\\t# the display is left to the console, which now reaches ttyGS0.\\n'\n"
     "        '\\t# Nothing is painted, so there is no fb0 write here that can go\\n'\n"
     "        '\\t# wrong, and the call sites below are unchanged.\\n'\n"
     "        '\\treturn 0\\n'\n"
     "        '}\\n'\n"
     "    ).encode()\n"
     "    assert replacement.count(_old_status) == 1, (\n"
     "        \"s5status is not the one the overlay removal was written against\"\n"
     "    )\n"
     "    replacement = replacement.replace(_old_status, _new_status, 1)\n"
     "    # The switch is gone with the thing it switched, and the variable that\n"
     "    # only existed to hold it goes too rather than being left reading as if it\n"
     "    # still controlled something.\n"
     "    _old_nos = 'nos5screen=\"${nos5screen:-y}\"\\n'.encode()\n"
     "    _new_nos = (\n"
     "        '# r26: nos5screen is gone with s5screen. There is no overlay left to\\n'\n"
     "        '# switch off, and a variable left behind would read as if it still did.\\n'\n"
     "    ).encode()\n"
     "    assert replacement.count(_old_nos) == 1, \"the nos5screen default moved\"\n"
     "    replacement = replacement.replace(_old_nos, _new_nos, 1)\n")

# And stop packaging the binary. The command line is unchanged and still has to
# supply seven arguments, so the path is still taken and the unpacking below still
# has to line up. All that changes is that it is no longer a file that goes into
# the image.
sub1('NEW_FILES = ("usr/bin/s5screen", "usr/bin/s5iskey", "usr/bin/s5usbkeep")',
     '# s5screen is deliberately absent in r26: the overlay is out of the image.\n'
     '# The path is still taken on the command line and still validated, so a\n'
     '# change to the file cannot slip past unnoticed while it is being ignored.\n'
     'NEW_FILES = ("usr/bin/s5iskey", "usr/bin/s5usbkeep")')
sub1('            "usr/bin/s5screen": s5screen_path,\n', '')

# Re-anchor the s5status checks. They are about init_functions.sh, where
# s5status is defined and where s5screen used to be invoked, and r26 now
# asserts the overlay really is gone rather than merely switched off.
sub1('    assert t.index("s5status() {") < t.index("s5status booting")\n    for state in ("booting", "dmok", "dmno", "loopfail", "keys", "booted"):\n        assert f"s5status {state}" in t, f"no call site paints the {state!r} state"\n',
     "    assert t.index(\"s5status() {\") < t.index(\"s5status booting\")\n"
     "    for state in (\"booting\", \"dmok\", \"dmno\", \"loopfail\", \"keys\", \"booted\"):\n"
     "        assert f\"s5status {state}\" in t, f\"no call site paints the {state!r} state\"\n")

sub1('    print(f"device tree{hashlib.sha256(dt).hexdigest()}  (unchanged)")',
     '    print(f"device tree{hashlib.sha256(dt).hexdigest()}  (bootargs patched)")\n'
     + DT_VERIFY)

# Constants for the patch, next to the other pinned base facts.
sub1('DT_SHA256 = "f6aac8da35c945bc982d10fccf29535a4692ea50669f5d323493c9c8b1228f6b"',
     'DT_SHA256 = "f6aac8da35c945bc982d10fccf29535a4692ea50669f5d323493c9c8b1228f6b"\n'
     '# Samsung wraps the flattened tree in a 2 KB "DTBH" blob header and DT_SIZE\n'
     '# counts the wrapper. Everything below works on the tree after it.\n'
     'DTBH_LEN = 2048\n'
     '# Pinned so the patch below cannot be applied to a different tree by accident.\n'
     'BASE_BOOTARGS = b"console=ttySAC2,115200 vmalloc=512M clk_ignore_unused\\x00"\n'
     '# Shorter than the base, which is what lets it fit without moving the tree.\n'
     '# No baud rate: a gadget serial port has no physical line to set a rate on,\n'
     '# and console=ttyGS0 with no options is the form Android uses for this very\n'
     '# port. console=ttySAC2,115200 is 23 bytes and console=ttyGS0 is 15, so the\n'
     '# corrected line is shorter and the property does not have to grow.\n'
     'NEW_BOOTARGS = b"console=ttyGS0 vmalloc=512M clk_ignore_unused\\x00"')

NEW = '''        # Step 2. r26's claims, checked against bytes and against the code.
        # Every r25 assertion is kept, because each one encodes a mistake that
        # actually shipped. The new ones cover the getty repair, the console and
        # the service matching, and they are anchored to whole lines.
        r26_init2nd = (HERE / "diagnostic-ramdisk-r26" / "init_2nd.sh").read_bytes()
        assert hashlib.sha256(r26_init2nd).hexdigest() == R26_INIT2ND_SHA256, (
            "the shipped r26 init_2nd.sh changed under this builder"
        )
        t = r26_init2nd.decode()
        lines = [re.sub(r"^\\s*#.*$", "", ln) for ln in t.splitlines()]
        code = "\\n".join(lines)
        assert "exec switch_root" in t, "init_2nd.sh no longer calls switch_root"
        switch = t.index("exec switch_root")

        def called(fn):
            n = "\\n%s\\n" % fn
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
             exact("low=$(echo \\"$name\\" | tr 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' "
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
             exact("cat > \\"$f\\" <<'STUB'")),
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

'''

a = s.index("        # Step 2.")
bnd = s.index("        # The keeper has to keep everything the base logger did")
s = s[:a] + NEW + s[bnd:]

pathlib.Path("source/repack-boot-r26.py").write_text(s)
print("  builder written for r26")
