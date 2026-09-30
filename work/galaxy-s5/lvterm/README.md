# lvterm - the LVGL Galaxy S5 terminal

The second of the two on-screen terminals. `s5term` is the one that was written
by hand; this is the same product built on a library instead, kept alongside it
so the two can be compared rather than argued about.

Both are on the phone:

| binary              | what it is                          | size     |
|---------------------|-------------------------------------|----------|
| `/usr/local/sbin/s5term` | hand written, pixels and evdev by hand | 78 KB |
| `/usr/local/sbin/lvterm` | this one, LVGL 9.5.0               | 5.4 MB   |

They both own `/dev/fb0`, so only one runs at a time, and killing one hands the
screen straight back. `lvterm` now starts itself at boot from one script in
`/etc/local.d` (see [Autostart](#autostart)); that is the only persistent change
on the phone, and deleting the file puts the boot back the way it was.

## What LVGL does for us, and what it does not

LVGL supplies the framebuffer display driver, the evdev input driver, and
`lv_keyboard` - a maintained QWERTY widget with modifier keys, popups and a
symbols layer, which is the single largest thing `s5term.c` had to implement by
hand. It also supplies layout, event dispatch and redraw scheduling.

It has **no terminal emulator widget**, so the pty, the line discipline and the
screen buffer are still ours. That is the part that is genuinely ours to get
right, and it is much smaller than `s5term`'s version because LVGL lays the text
out rather than us blitting glyphs.

The font is ours too. No bundled LVGL font is monospace, and a terminal needs
monospace, so the 16x32 VGA bitmap from the PSF2 that `s5term` already uses is
fed to LVGL through the standard `fmt_txt` descriptor. It needs no conversion: a
PSF2 16x32 1bpp glyph is 32 rows of 2 bytes, MSB leftmost, 64 bytes, which is
exactly the layout LVGL expects, so the existing `font16x32.h` data is reused as
is.

The keyboard gets Montserrat instead, because its keys are labelled with
`LV_SYMBOL_BACKSPACE`, `LV_SYMBOL_NEW_LINE` and arrows, which live above U+2000
where a VGA 8-bit font has nothing to show.

## The one patch that matters

`/dev/fb0` here is not one screen of memory. `smem_len` is 16588800 bytes,
exactly twice 1080x1920x4, because the driver keeps two stacked screen buffers
and pans between them; the stock `msm-fb-refresher` service drives that pan on a
loop. LVGL's fbdev driver maps a single screen and writes each dirty row once,
so every pan lands the viewer on a half that was never painted.

`apply-lvgl-patches.py` makes it map all of `smem_len` and write every flushed
row into every buffer. The confirmation is in the log at startup:

```
lv_linux_fbdev_set_file: panel memory holds 2 stacked screen buffers,
                         painting all of them
```

Measured after the patch: **0 framebuffer changes in 20 s** on the idle terminal
screen, with the refresher still running. The refresher is left alone, on
purpose - it is a hard dependency in `device-samsung-k3gxx/APKBUILD` and stopping
it would be trading a cosmetic problem for a broken display.

The second patch in that file, widening the evdev capability mask from 32 bits,
is insurance rather than a rescue. See the comment in the script: the
`ABS_X`/`ABS_Y` bits it fails to check are still inside the 32 bit window on
this device. It is kept because it makes the library correct for a digitiser
that publishes only `ABS_MT_POSITION_X/Y`.

## Idle is silent

The refresher was never the flicker, though. It only pans the double buffer; two
other writers repaint the picture on their own. Both are fixed:

- **The kernel console cursor.** fbcon stays attached to `/dev/fb0` while
  lvterm runs, and its cursor keeps blinking over whatever we drew. It was
  identified by its fingerprints rather than guessed at: it writes `0x00RRGGBB`
  where LVGL writes `0xffRRGGBB`, it is one VGA console character in size, and
  it sits at the very bottom of the screen (row 119 of the console's 120 rows)
  - both facts matched the pixel patch exactly. Proving it was the kernel
  cursor, not a drawing bug: `setterm -blink off` on `/dev/tty0` moved the
  blinking patch to a new position, exactly where the console cursor went. The
  fix is the standard one for a fullscreen program: take the console into
  `KD_GRAPHICS` mode on `/dev/tty0` at startup and hand it back (`KD_TEXT`) on
  exit, both failing open. A refused mode change just leaves the cursor
  blinking, which is the cosmetic bug this fixes, not a reason to die for.
- **The textarea cursor.** On the terminal screen, the input line's cursor blinked at the
  default theme's 400 ms, repainting its cell twice a second. The theme sets
  that period as the `anim_duration` style on `LV_PART_CURSOR | LV_STATE_FOCUSED`,
  which is why the override has to carry the same selector; set to 0, the
  cursor stops blinking and stays as a steady block - the same information,
  none of the churn.

With both fixed, idle is silent in every state of the screen, measured on the
committed build (sha256 `71962728…`, selftest ALL PASS, tap regression green):

| screen state                  | idle traffic                        |
|-------------------------------|-------------------------------------|
| terminal, keyboard hidden     | 0 changes in 20 s (200 ms sampling) |
| terminal, keyboard up         | cursor only, by design              |

The terminal screen was also measured at the finer 50 ms sampling earlier in the
same session (0 changes in 25 s) for the same result. The keyboard-raised state
is the one deliberate exception: that is the state for typing, and the only
thing that moves there is the cursor over the text, which is the point of the
screen.

## Build

```
make            # 464 objects, static arm-linux-musleabi binary
make patch      # re-apply the LVGL fixes to scratch-lvgl/patched
make diff       # write lvgl-patches.diff against the pristine tree
make clean
```

The toolchain is the vendored zig in `../scratch-toolchain`, the same one that
builds `s5term`, `fbdump`, `fbwatch`, `absprobe` and `fbpeek`.

LVGL itself comes from the exact commit pmaports pins for `buffybox`
(`85aa60d18b3d5e5588d7b247abf90198f07c8a63`, LVGL 9.5.0), so this is the same
library version postmarketOS already builds a phone UI against, and the
tarball's sha512 matches the one in `buffybox`'s APKBUILD.

## Run

Needs root: `/dev/fb0` is `root:video 0660` and `/dev/input/event2` is
`root:input 0660`.

```sh
sudo pkill -x lvterm          # or s5term, if that is what is running
sudo /usr/local/sbin/lvterm > /var/log/lvterm.log 2>&1 &
```

## The button band

Three keys sit in a row of their own under the input line, 48 px tall, splitting
the full width three ways:

| key    | x         | what it does                             |
|--------|-----------|------------------------------------------|
| `CTRL` | 0..356    | sticky control, for the very next key     |
| `^C`   | 357..713  | interrupt whatever the shell is running   |
| `LOCK` | 714..1070 | turn the panel off                        |

48 px rather than the one glyph row (32) they used to share with the input line,
because a fingertip on a 32 px target is a coin toss and these are the keys that
matter when something has gone wrong. Height is not free: the band needs a row
of its own, since a 48 px band starting at the input line's top edge covers the
text being typed, and one starting at its bottom edge covers the keyboard's
first row of keys. So the visible output is 27 rows now, against the 29 it was
when `^C` and `LOCK` sat beside the input line. Width was free by contrast - the
input line is a fixed-size label, not a grid the text has to line up with, so it
keeps the full terminal width and the buttons take all of it.

### CTRL is a latch, not a chord

`CTRL` arms a modifier and the next key pressed consumes it:

- tap `CTRL` - armed, and the caption reads `CTRL ON`
- tap any key - that key is combined with control, and the latch clears itself
- tap `CTRL` again - cancelled, nothing sent

A latch rather than a hold, because nothing on a touchscreen reports that a key
is still down. `lv_keyboard` swallows its own shift and arrow keys rather than
reporting them as modifiers, so there is no key event to hold a modifier
against - and a modifier that could be left stuck down would be a good deal
worse than one that cannot.

What it combines with is the keyboard and the `^C` button, and the byte goes
**straight to the pty, never into the input line**. That is the whole point of
it: the input line is a local buffer that only reaches the shell on Enter, so a
control character typed into it would sit there doing nothing. Writing to the
master is what lets `^D` reach a shell sitting at its prompt, and `^C` reach a
command that has stopped listening, without submitting anything.

| key                          | byte             | meaning                     |
|------------------------------|------------------|-----------------------------|
| `a`..`z`, `A`..`Z`           | 0x01..0x1a       | ^A..^Z                      |
| space                        | 0x00             | NUL                         |
| `[` `\` `]` `^` `_`          | 0x1b 0x1c..0x1f  | ESC, FS, GS, RS, US         |

Case is dropped rather than treated as distinct, because on a real terminal
Ctrl-Shift-C is ETX and not "Ctrl-C with a shift on it".

Three kinds of key deliberately send nothing, and all of them clear the latch
instead:

- a key with no control meaning - a digit, a full stop - is typed as itself, so
  arming by mistake never eats a character
- backspace and the arrows do their own job: there is no C0 character a
  backspace key means, and a bare escape sequence sent to a shell sitting at its
  prompt does nothing you could see
- `^C` is already Ctrl-C, so the combination would be a no-op that still has to
  clear the latch - it sends and disarms. `^D` is one tap away as `CTRL` then
  `d`, and the table above covers the rest.

The latch also clears when the keyboard is dismissed, when the screen is locked,
and on the wake, so it can never outlive the context it was armed in - an armed
`CTRL` surviving a dark screen would swallow the first key pressed after it.

Every control byte sent is logged unconditionally (`ctrl: sent 0x03`) rather
than under `LVTERM_TRACE`, because it is the one thing this program does that
changes nothing on the glass, and a press that went somewhere unexpected has to
leave a trace by default. The `diag` line carries `ctrl=` for the same reason.

### The lock key

`LOCK` turns the panel off, and any tap turns it back on to the terminal with
the keyboard up.

The screen-off is the panel's own backlight, `/sys/class/backlight/panel/brightness`,
written to 0 and restored to the value saved at the moment of the lock. `FBIOBLANK`
is the obvious alternative and is not usable here: the kernel console owns the
blanking, and `ioctl(FBIOBLANK)` fails with `Resource busy` (it is in the log at
every start).

What makes the wake work without a visible button is that the digitiser has its
own supply. Backlight 0 does not stop the touchscreen reporting, so the tap
arrives while the screen is dark; every widget is hidden at that point, which
leaves the screen object itself as the only thing under the finger, and its
`tap_handler` is the wake. Nothing on the glass has to be aimed at.

Both directions fail open. If the backlight node cannot be read or written, the
widgets still hide and show, which is the part a person can see, and an
unreadable value falls back to brightness 133. The exit path and the fatal
signal handler both restore the panel before handing the console back, so a
crash with the screen off cannot leave the phone dark with nothing but a power
cycle to fix it. A start with the node already at 0 lifts it, for the same
reason.

The shell keeps running the whole time. Waking does not reconnect anything,
because nothing was disconnected.

## Autostart

`/etc/local.d/lvterm.start` is the whole of it - one executable file, root-owned
mode 755:

```sh
sudo chmod 755 /etc/local.d/lvterm.start     # the local service skips non-executables
```

OpenRC's `local` service runs the `*.start` files in that directory, and it is
last in the default runlevel with `depend { after * }`, so it runs after the
console login is already on the framebuffer. lvterm then takes `/dev/fb0` over
cleanly instead of racing the kernel console for it. No runlevel change was
needed: `local` was already enabled.

It skips the start if one is already running, because two lvterms on one
framebuffer is a flickering mess, and after two seconds it records which of the
two things happened - it came up, or it died - in `/var/log/lvterm.autostart`,
so "it came up" is a fact rather than a belief:

```
2026-09-29 04:02:57 autostart: lvterm running (pid 7692)
```

That marker is a separate file on purpose. The obvious `>> "$LOG"` is a trap:
lvterm holds `/var/log/lvterm.log` open on its own file offset, and two writers
at different offsets means the next line lvterm writes lands before the marker
and overwrites it - so the marker would survive or vanish depending on timing,
which is worse than not having it. A check that is usually right teaches you to
trust a thing that is not always true. Separate file, separate writer, no race.

The script also sets the shell's environment, and that is not decoration. It is
eval'd by OpenRC, so it inherits the *service* environment: `PWD=/`, no `HOME`,
no `USER`, and a `PATH` pointing at `/usr/libexec/rc`. lvterm's pty child
inherits whatever it has and execs `/bin/sh` without `-l`, so nothing sources
`/etc/profile`, and the result is busybox's built-in fallback prompt with
nothing to fill it in:

```
/ #
```

A working shell, but a stranger's: no `PATH` worth the name and no home
directory. The fix is to hand lvterm the environment a console login would give
- `PWD=/home/user`, `HOME=/root` (the shell really is root, it is forked by a
root process, so it has no business pretending otherwise), and the normal
`PATH`. It is a handful of `export`s in the script rather than a change to
`pty_start`, because this is a property of *how lvterm was started*, and the C
has no way to know it should not also be true of a hand start from ssh.

That is also why the fixup has to happen in a subshell, and it is the one part
of this that is easy to get wrong in a way that still looks fine. The script is
eval'd *inside* the local service's own shell, so unsetting `RC_SVCNAME` and its
siblings in place would leave the service without the bookkeeping it needs to
finish starting - the boot would go quiet in a far more confusing place than a
wrong prompt. A `launch() ( ... )` subshell keeps the whole fix local to the
process being started.

The `RC_*`, `EINFO_*` and `SUDO_*` variables are dropped for the same reason: at
boot OpenRC runs this as root and they are absent, so leaving them set on a
manual start would make the two paths differ in exactly the way that caused the
problem. Worth knowing that this whole class of defect is invisible from a hand
start - lvterm run over ssh inherits that session's environment, so the shell
looks right for a reason that has nothing to do with the binary. The `RC_*` leak
only ever appears on the path OpenRC actually takes.

Removing it and rebooting puts the phone back to the console login:

```sh
sudo rm /etc/local.d/lvterm.start
```

To run the same path without rebooting, note that OpenRC refuses to start a
service it already considers started, so call the script itself:

```sh
sudo pkill -x lvterm
sudo /etc/local.d/lvterm.start
```

## Verifying without looking at the screen

There is no way to see the phone's display from here, so the checks are
numeric. `fbpeek` is the one that made this workable:

```sh
sudo /tmp/fbpeek /dev/fb0 --map            # the screen as ASCII art
sudo /tmp/fbpeek /dev/fb0 --map 54 45 0 928 # just the terminal half, rows 0..927
sudo /tmp/fbpeek /dev/fb0 --top            # commonest pixel values
sudo /tmp/fbwatch 20                       # fb changes over 20 s at 50 ms
sudo /tmp/fbwatch 20 200                   # same, at 200 ms sampling
```

`fbwatch` takes the seconds as its first argument and the sample interval in
milliseconds as its second; `/dev/fb0` is compiled in.

The `--map` output is a luminance downsample of the live framebuffer. It is the
difference between "0 changes" meaning *stable* and "0 changes" meaning *painted
nothing*, and it is how the layout was checked without a person looking at
hardware. Naming the row range (`--map 54 45 Y0 Y1`) makes the terminal half
legible instead of a 45-row average of the whole screen. `lvterm` also prints a
`diag` line every ten seconds with object coordinates and hidden state, for the
same reason.

The self test replaces the finger. `LVTERM_SELFTEST=1` drives the real input
path - pty, line discipline, screen buffer, the lot - with two scripted lines
and reports PASS/FAIL for each link:

```sh
sudo sh -c 'LVTERM_SELFTEST=1 /usr/local/sbin/lvterm >/tmp/selftest.out 2>&1 &'
sleep 8 && cat /tmp/selftest.out     # stage 1 PASS saw_uname=1 saw_linux=1
                                     # stage 2 PASS masked=1
```

Taps can be scripted too. Run lvterm with `LVTERM_TAPLOG=/tmp/taps` and a
file of `x y` lines (one tap per main-loop pass); each tap is logged as
`vtap N: press/release at x y`. More lines can be appended while it runs, which
is how the lock cycle is checked one half at a time:

```sh
echo "969 943" > /tmp/taps          # the LOCK button
sudo sh -c 'LVTERM_TAPLOG=/tmp/taps /usr/local/sbin/lvterm >/var/log/lvterm.log 2>&1 &'
sleep 3; cat /sys/class/backlight/panel/brightness          # 0
echo "540 400" >> /tmp/taps         # any other point on the glass
sleep 3; cat /sys/class/backlight/panel/brightness          # 133
```

The reading is the check that matters: the backlight lives outside the process,
so a lock that never reached the panel would look identical on the glass. The
log carries the same information in the lines `lock: panel off, brightness 133
-> 0` and `wake: panel on, brightness 133`, with `off=1` then `off=0` on the
`diag` lines and every widget flipping to `hidden=1` and back.

The rest of the touch regression is the keyboard-raise tap, `i`, `d`, Enter,
Ctrl-C, and the keyboard toggle - checked in `lvterm.log` markers: the typed
text (`ta_changed text='id'`), the submit (`sent=3`, then `uid=0(root)` on the
screen), the interrupt (`intr=1`) and the keyboard hiding again (`kb hidden=1`).

Note that `fbdump` reports "PANEL IS BLANK" for a screen full of content when
the program writing it uses a non-zero alpha byte: it compares exact
`0x00RRGGBB` values, and LVGL writes `0xffRRGGBB`. `fbpeek` ignores the top byte
for that reason.

## Current state

All verified on the phone, build `71962728…` (sha256
`71962728d7ba99d51cff561fece80399bd30369a15afab98d8131f9bd2da0611`, static
arm-linux-musleabi, selftest ALL PASS):

- starts, maps `/dev/fb0`, reports 2 stacked buffers
- touch node registered as a pointer indev (`indev: type=1`), 1080x1920
- pty shell started, prompt rendered; `id` run and echoed through the input path
- comes up on the terminal with the keyboard hidden, no wake screen
- **0 framebuffer changes idle**, `msm-fb-refresher` running
- kernel console handed to `KD_GRAPHICS` at start, back to `KD_TEXT` on exit;
  the console cursor no longer blinks over the picture
- input-line cursor is a steady block (400 ms theme blink disabled); the only
  moving pixel on the screen is the cursor while it is being moved
- **lock key**: `btn_lock` at x 932..1007, scripted tap turns the panel off
  (133 -> 0, every widget `hidden=1`, `off=1`)
- **wake**: a tap anywhere brings it back to the terminal with the keyboard up
  (brightness 133 restored, `off=0`, all widgets `hidden=0`)
- **autostart**: after a real reboot, lvterm was running at 150 s uptime,
  started by `/etc/local.d/lvterm.start` (`autostart: lvterm running` in the
  log), with `sec_touchscreen` bound to `event2`, so the boot was a normal one
  and touch is live
- the start script does not start a second copy when one is already running,
  and does start one when a previous instance was killed a moment earlier (a
  dying lvterm still holds `/dev/fb0`, so the guard waits it out)
- the shell comes up as a normal login, not as an OpenRC service shell: prompt
  `/home/user #`, `PATH` the standard one, no `RC_*` or `SUDO_*` in the
  environment, and selftest ALL PASS through it

The phone is left **running lvterm normal** as `/usr/local/sbin/lvterm`, started
by the autostart script at boot, sitting on the terminal with a live shell. No
user data, EFS, modem, bootloader, recovery or external SD was touched to make
any of this work.
