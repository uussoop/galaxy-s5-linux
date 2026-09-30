# Temporary BOOT diagnostics

The immutable candidate is `../../boot-k3gxx-r11-diag-31e2fcce.img` (SHA-256
`31e2fccefa297c5eadf8d69bc4f91e9dfce29d47a8601cbd83eb41363887b5ae`,
13,301,760 bytes). It is generated from the verified r1 BOOT image with:

```sh
python3 artifacts/source/repack-boot-diagnostic.py \
  artifacts/boot-k3gxx-r11-47e72417.img \
  artifacts/source/diagnostic-ramdisk \
  artifacts/boot-k3gxx-r11-diag-31e2fcce.img
```

The source r1 BOOT SHA-256 is
`47e7241722b5d87508be4f25f796acb338a7bc4f6dd77ebaf9da70674eed660b`.
The diagnostic BOOT retains its exact kernel (SHA-256
`6d0c337411dcbfe6771347446af8fcab19bb5bff265496a5cddaa272de631b8d`)
and DT (SHA-256
`f6aac8da35c945bc982d10fccf29535a4692ea50669f5d323493c9c8b1228f6b`).
Only `init` and `init_2nd.sh` change in the CPIO archive; `usr/bin/s5diag` is
added. The gzip ramdisk SHA-256 is
`be4e8c29108aad3bbba4088f1b905622c5d02bdd69332c2fe5035e86305954b4`.
The Android image ID is recalculated, and the result is 329,728 bytes below the
13,631,488-byte BOOT partition limit.

The logger runs asynchronously in initramfs. It requires physical CACHE p19 to
match partition number 19, start sector 5,406,720, size 409,600 sectors, MMC
parent `mmcblk0`, and ext4 UUID `57f8f4bc-abf4-655f-bf67-946fc0f9f25b`
before mounting it. It does not format any partition. It creates mode-0700
per-boot directories under `/codex-s5-diagnostics`, writes mode-0600 bounded
snapshots (`kernel.log`, `initramfs.log`) every five seconds, and unmounts CACHE
before switch_root. A `stage` file marks arrival at switch_root. All diagnostic
errors are fail-open, with no synchronous partition wait in PID 1. Existing
installed rootfs and its private Wi-Fi/SSH material are unaffected.

This image is for a bounded diagnostic boot only. Subsequent BOOT images should
remove the CACHE logger once the boot stage is understood.
