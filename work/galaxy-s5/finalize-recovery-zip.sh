#!/bin/sh
# Repack the already built pmbootstrap recovery ZIP after rootfs-only overlays.
set -eu

cd /var/lib/postmarketos-android-recovery-installer
tar -pcf rootfs.tar --exclude ./home -C /mnt/rootfs_samsung-k3gxx .
tar -prf rootfs.tar -C / ./etc/apk/keys
gzip -f1 rootfs.tar
build-recovery-zip samsung-k3gxx
