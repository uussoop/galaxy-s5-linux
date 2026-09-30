#!/bin/sh
set -eu

s5_root=/home/pmos/work/chroot_rootfs_samsung-k3gxx
s5_overlay=/tmp/s5-overlay
s5_firmware=/tmp/s5-vendor-firmware
s5_public_key=/tmp/s5_ed25519.pub

test -d "$s5_root/etc"
test -s "$s5_public_key"

sudo install -Dm600 "$s5_overlay/etc/ssh/sshd_config.d/40-s5-key-only.conf" \
	"$s5_root/etc/ssh/sshd_config.d/40-s5-key-only.conf"
sudo install -Dm600 "$s5_overlay/etc/doas.d/99-s5-user.conf" \
	"$s5_root/etc/doas.d/99-s5-user.conf"
sudo install -Dm644 "$s5_overlay/etc/conf.d/unudhcpd.ncm0" \
	"$s5_root/etc/conf.d/unudhcpd.ncm0"
sudo install -Dm644 "$s5_overlay/etc/conf.d/sshd" \
	"$s5_root/etc/conf.d/sshd"
sudo install -Dm755 "$s5_overlay/etc/init.d/s5-usb-ncm" \
	"$s5_root/etc/init.d/s5-usb-ncm"
sudo install -Dm755 "$s5_overlay/etc/init.d/s5-wifi-init" \
	"$s5_root/etc/init.d/s5-wifi-init"
sudo install -Dm755 "$s5_overlay/etc/init.d/s5-wifi" \
	"$s5_root/etc/init.d/s5-wifi"

sudo install -d -m700 "$s5_root/etc/skel/.ssh"
sudo install -m600 "$s5_public_key" "$s5_root/etc/skel/.ssh/authorized_keys"
sudo chown root:root "$s5_root/etc/skel/.ssh" "$s5_root/etc/skel/.ssh/authorized_keys"
sudo chmod 0700 "$s5_root/etc/skel/.ssh"
sudo chmod g-s "$s5_root/etc/skel/.ssh"
sudo chmod 0600 "$s5_root/etc/skel/.ssh/authorized_keys"

for s5_file in bcmdhd_sta.bin bcmdhd_apsta.bin nvram_net.txt \
	nvram_net.txt_semco3rd nvram_net.txt_wisol; do
	sudo install -Dm644 "$s5_firmware/$s5_file" \
		"$s5_root/system/etc/wifi/$s5_file"
done
# Factory CID is semco3rd; the built-in driver loads this default path first.
sudo install -m644 "$s5_firmware/nvram_net.txt_semco3rd" \
	"$s5_root/system/etc/wifi/nvram_net.txt"

sudo ln -sfn /etc/init.d/unudhcpd "$s5_root/etc/init.d/unudhcpd.ncm0"
sudo rm -f "$s5_root/etc/runlevels/boot/unudhcpd.usb0"
sudo ln -sfn /etc/init.d/s5-usb-ncm "$s5_root/etc/runlevels/boot/s5-usb-ncm"
sudo ln -sfn /etc/init.d/unudhcpd.ncm0 "$s5_root/etc/runlevels/boot/unudhcpd.ncm0"
sudo ln -sfn /etc/init.d/s5-wifi-init "$s5_root/etc/runlevels/default/s5-wifi-init"
sudo ln -sfn /etc/init.d/s5-wifi "$s5_root/etc/runlevels/default/s5-wifi"
sudo ln -sfn /etc/init.d/dbus "$s5_root/etc/runlevels/default/dbus"
sudo ln -sfn /etc/init.d/avahi-daemon "$s5_root/etc/runlevels/default/avahi-daemon"

# These locally patched boot packages need an intentional rebuild before upgrade.
sudo sed -i '/^device-samsung-k3gxx/d; /^linux-samsung-k3gxx/d; /^postmarketos-initramfs/d' \
	"$s5_root/etc/apk/world"
printf '%s\n' \
	'device-samsung-k3gxx=4-r2' \
	'linux-samsung-k3gxx=3.10.9-r0' \
	'postmarketos-initramfs=3.12.3-r3' | sudo tee -a "$s5_root/etc/apk/world" >/dev/null

echo "Staged key-only SSH, doas, USB NCM, Wi-Fi startup, Avahi, and stock Wi-Fi firmware."
