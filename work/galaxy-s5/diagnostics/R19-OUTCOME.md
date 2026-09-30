# r19 outcome — CACHE keeper did not hold acm

Date: 2026-09-28
Image: `artifacts/boot-k3gxx-r19-cachelog-242091ca.img` (md5 `2b4324e36713d1a5312ae6269573e6b0`)

## Verdict

**r19 failed.** The USB serial console came up and was then taken away. The
initramfs half of the design is proven correct; the failure is entirely after
`switch_root`, and the post-`switch_root` keeper did not write anything.

## Evidence, from the host, passively

`scripts/usb-descriptor-watch.py` reads the phone's USB descriptor via `ioreg`.
android.c publishes `bcdDevice` = 0x0400 when acm is bound and 0xFFFF when it is
not, so the host can tell which is on the wire without touching the phone.

    [01:49:51] SERIAL CONSOLE LIVE  bcdDevice=1024 serial=01f44ecab714 nodes=['cu.usbmodem01f44ecab7141']
    [01:50:01] acm LOST, ncm only   bcdDevice=65535 serial=01f44ecab714 nodes=[]

- 1024 = 0x0400 = acm. A live `/dev/cu.usbmodem01f44ecab7141` node appeared,
  carrying the phone's own descriptor serial.
- 65535 = 0xFFFF = ncm. The serial node vanished.
- Interface dump taken while ncm: interface 0 class 2 / subclass 13 (CDC NCM),
  interface 1 class 10 (CDC Data). No CDC ACM interface. Byte-for-byte the r18
  end state.

**The keeper did not re-assert.** A 90 s burst sampling every 0.2 s produced 401
samples: 401 ncm, 0 acm. A keeper that was alive but losing a fast race would
still have shown acm samples. It wrote nothing, so it either died at
`switch_root` or never received the handover.

## The phone's own screen

    INFO: android_usb functions: [acm]
    INFO: android_usb enable: [1]
    ...
    Switching root
    [pmOS-rd] Disabling console output again (use 'pmOS.debug-shell' to keep it enabled)
    Welcome to postmarketOS
    Kernel 3.10.9-LineageOS on an armv7l (/dev/tty1)
    galaxy-s5 login:

- The real system booted cleanly and reached a login prompt on tty1. USERDATA
  decrypted, root mounted, getty running. Nothing is broken in the real system.
- Immediately before `switch_root`, android_usb was still `[acm]`, matching r18
  exactly. So the initramfs handed over correctly in both revisions.
- The keeper's absence from the screen proves nothing: the real system disables
  console output at handover, so kmsg writes would not be visible.
- Useful detail, from the initramfs itself: `controllers available:
  [12000000.dwc3 12400000.dwc3 ]`. There are two dwc3 controllers registered.

## Where the search stands

Unchanged from the r18 conclusion and now confirmed on hardware: the rebind
happens after `switch_root`, and it is done either by Samsung's in-kernel
`conn_gadget` or by the real userland re-running USB setup. r19 was built to
tell those apart by writing the 25 ring-buffer lines preceding each re-assert
into CACHE. That evidence is on the phone and has not been read yet.

`stage` answers the keeper's fate in one word: `CACHE keeper alive` or
`CACHE keeper gone`. That is the next thing to pull.

## Two design consequences, independent of what usb.log says

1. **A keeper that relies on outliving `switch_root` is the wrong tool.** Even if
   the rebind is a one-shot userspace action, depending on the old root and CACHE
   mount surviving is a fragile premise that has now failed once. The keeper
   belongs in the real system, where it cannot be torn down. The initramfs mounts
   `/sysroot` read-write and recovers the journal, so the real rootfs is writable
   during the initramfs phase and an inittab line plus a small script can be
   installed there. That is a reversible one-line change, and it needs the user's
   consent because it touches the installed system.

2. **Two dwc3 controllers exist.** If the real system drives only one of them,
   the other is untouched by whatever is rebinding android_usb, and acm bound on
   the idle controller would never be contested. Worth testing if the real-system
   keeper is not sufficient. Which physical connector each controller reaches is
   not yet established.

## Procedure note

`scripts/serial-watch.py` reads from the serial port (`os.read`), so running it
while a user types the root password would capture the password. It was killed
for this reason. `scripts/usb-descriptor-watch.py` only calls `ioreg` and lists
`/dev/cu.*`, never opening the port, and is the safe instrument to leave running.
