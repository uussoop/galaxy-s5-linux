# Native subpartition boot candidate

`../../boot-k3gxx-r11-subpartfix-35de59d0.img` is SHA-256
`35de59d0d876747e6e00ff7a63887af92e5c02baf3614a075781795a55094a60`,
13,301,760 bytes, leaving 329,728 bytes in the 13,631,488-byte BOOT partition.
Reproduce it from the immutable diagnostic image:

```sh
python3 artifacts/source/repack-boot-subpartfix.py \
  artifacts/boot-k3gxx-r11-diag-31e2fcce.img \
  artifacts/source/subpartition-ramdisk/init_functions.sh \
  artifacts/boot-k3gxx-r11-subpartfix-35de59d0.img
```

Only `init_functions.sh` changes in the CPIO archive: remove
` --direct-io=on` from `mount_subpartitions()`'s `losetup_args`. The r1 kernel,
DT, and CACHE diagnostic logger are byte-identical to the preceding image;
the Android boot image ID is recomputed. The installed root filesystem is not
changed by flashing this BOOT candidate.

The native Linux log showed loop0 failing to read sector zero of the verified
USERDATA p21 nested MBR while `losetup --direct-io=on` was active. A read-only
comparison under TWRP's separate 3.10 kernel succeeded with both direct I/O on
and off, so that probe does not establish the cause on the native kernel.
This image is a bounded native test of the least invasive change.

For persistence after hardware validation, local `postmarketos-initramfs`
source is bumped to 3.12.3-r4 with the same flag removal. The signed APK is
`../../postmarketos-initramfs-3.12.3-r4-444e67d9.apk` (SHA-256
`444e67d9858a649ce6882400336bfb928a3b16a8454a9299783e3599be015c2c`).
The public signing key is `../../pmos@local-6ab8c176.rsa.pub` (SHA-256
`f74de7179f9d34ff777d5cccdf95fcc0255d554d738a9b9d37a294b1f58b81ff`).
The prior r3-to-r4 source delta is `../postmarketos-initramfs-r4.patch`.

The installed native rootfs currently pins kernel 3.10.9-r0 and initramfs
3.12.3-r3. Before any future `mkinitfs` deployment, update both pins and
install the verified r1 kernel APK and r4 initramfs APK together. Otherwise
regeneration can restore the old kernel or `--direct-io=on`.
