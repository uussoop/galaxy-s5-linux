# Native subpartition boot via device-mapper

`../../boot-k3gxx-r11-dmsubpart-d0542337.img` is SHA-256
`d0542337fe915d79291e77746aacba3067f079faca4da7b970a1ecd3fdec8094`,
13,303,808 bytes, leaving 327,680 bytes in the 13,631,488-byte BOOT partition.
Its ramdisk is SHA-256
`28c0f107f65987ecd4bfa0153a72e2375bb5edea240daafdd109f86638d62a7d`,
5,903,128 bytes. Reproduce and verify it from the installed image with:

```sh
python3 artifacts/source/repack-boot-dmsubpart.py \
  artifacts/boot-k3gxx-r11-subpartfix-35de59d0.img \
  artifacts/source/dm-ramdisk/init_functions.sh \
  artifacts/boot-k3gxx-r11-dmsubpart-d0542337.img

python3 artifacts/source/verify-boot-dmsubpart.py \
  artifacts/boot-k3gxx-r11-subpartfix-35de59d0.img \
  artifacts/boot-k3gxx-r11-dmsubpart-d0542337.img \
  artifacts/source/dm-ramdisk/init_functions.sh
```

## Why

The base image is the currently installed and verified candidate
`../../boot-k3gxx-r11-subpartfix-35de59d0.img` (SHA-256 `35de59d0…`), so this
image differs from what is on the phone by exactly one variable. The native
kernel cannot read LBA 0 of the nested MBR through a loop device: every attempt
ends in `Buffer I/O error on device loop0, logical block 0` plus `loop0:
unable to read partition table`, while the mmc stack itself reports no error.
Direct I/O on and off both fail, which the fifth boot established, so the flag
is not the cause.

The same nested partitions read fine under recovery, where kpartx builds plain
device-mapper linear tables over the identical extents:

```
0 497664   linear 259:13 2048      -> pmOS_boot, ext2
0 24346624 linear 259:13 499712    -> pmOS_root, ext4
```

So the initramfs now builds the same tables itself, and losetup stays the
fallback. If any guard does not match, the mappings are removed again and
`mount_subpartitions()` continues exactly as it did before.

## What changes

Only `init_functions.sh` changes in the CPIO archive, 40,694 to 50,533 bytes,
inserted as two regions delimited by `# >>> dm-bypass: … (begin)` and
`# <<< dm-bypass: … (end)`. Stripping those two regions reproduces the base file
byte for byte, which both `check-delta.py` and the repack script assert, so an
edit outside them cannot slip into an image. The r1 kernel, the DT, `init` with
its CACHE diagnostic logger, and the `losetup --show -Pf` fallback are
byte-identical to the base; only the boot image ID and the ramdisk size are
recomputed.

## Guards

Nothing is remapped unless all of the following match, and `PMOS_BOOT` /
`PMOS_ROOT` are pinned to the created nodes rather than looked up afterwards,
because util-linux blkid only searches device-mapper devices that received
major 253 and this kernel hands them 254:

- a real partition of the internal mmc disk, addressed in 512 byte sectors;
- exactly two MBR entries, Linux type 83, ascending, not overlapping, starting
  at or after LBA 2048 and ending no further than the end of the partition;
- per mapping: sysfs `size` equals the requested sector count, `dm/name`
  equals the mapping name, and `slaves/` contains the parent partition;
- per mapping: `blkid` reports `pmOS_boot`/`ext2` or `pmOS_root`/`ext4`;
- a mapping name that is not already taken, one boot and one root, and neither
  `PMOS_BOOT` nor `PMOS_ROOT` already pinned.

A failure at any point calls `dm_remove_subpartitions()`, which removes every
mapping the attempt created along with its device node.

`dm_control_node()` builds `/dev/mapper/control` itself: the kernel registers
the control device in `/proc/misc` as `device-mapper` minor 236 and devtmpfs
exposes it as `/dev/device-mapper`, and the initramfs has no mdev rule for it.
Only that exact name is accepted, and only if it yields a single bare number, so
an unrelated misc device can never be turned into a node. Both device nodes are
created with mode 0600.

## Evidence

Six read-only probes on the phone, run from TWRP with the initramfs' own
musl binaries as the userland (`probe-initramfs-tools.sh`, `probe-dm-table.sh`,
`probe-blkid-search.sh`, `probe-blkid-mmcref.sh`, `probe-sysfs-nodes.sh`,
`probe-sysfs-dm.sh`) established: `dmsetup` and `libdevmapper.so.1.02` are in
the initramfs; busybox `fdisk -l` MBR lines have the field layout
`dm_mbr_entries()` assumes; `blockdev --getss` is 512; device-mapper devices
are not registered under `/sys/class/block/mapper` and are resolved through
`/sys/dev/block/<major>:<minor>`; and util-linux blkid 2.42.4 returns empty for
every search mode on major 254 while `blkid <node>` works. Each probe created
only temporary in-memory tables and removed them again.

`test-dm-bypass.sh` then ran the functions verbatim against the real nested
USERDATA partitions, with one TEST-ONLY change: `dm_attach_one()` names its
mappings `s5t<base>p<n>`, because kpartx already owns the real names for the
maintenance mounts. The last run passed all five stages:

1. `dm_control_node()` recreated a working `crw------- 10, 236` control node
   after the original was moved away;
2. `dm_attach_subpartitions()` produced tables identical to the kpartx ones,
   confirmed `pmOS_boot`/`ext2` and `pmOS_root`/`ext4` by blkid, and both nodes
   read 1024 bytes of data;
3. a second call was refused and left the live mapping in place;
4. `mmcblk0p18`, `p19` and `p20` were all refused;
5. cleanup left the kpartx maintenance mappings untouched.

TWRP has no `/bin/sh`, and the sourced file uses process substitution in
`setup_log()`, so the test has to be run by the initramfs busybox:

```sh
adb shell 'C=/tmp/codex-s5-maint/chroot; D=/tmp/codex-s5-probe; L=$C/lib/ld-musl-armhf.so.1; "$L" --library-path "$C/lib" "$D/busybox" ash "$D/dmtest.sh"'
```

`init_functions.test.sh` is generated from `init_functions.sh` by a single
substitution, the TEST-ONLY name prefix; `diff` between the two shows only
that line. The shipped `init_functions.sh` was additionally checked with
`busybox ash -n` on the phone, because macOS `sh -n` rejects the pre-existing
`exec > >(tee …)`.

If this boot reaches `switch_root`, remove the diagnostics afterwards as
described in `../../recovery-maintenance/README.md`. For persistence beyond
this hardware validation the same change belongs in the local
`postmarketos-initramfs` source; the signed r4 APK currently in
`../../postmarketos-initramfs-3.12.3-r4-444e67d9.apk` does not contain it and
must not be installed as the fix.
