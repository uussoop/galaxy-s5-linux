# Galaxy S5 native Linux installation

User explicitly wants Android removed and phone fully wiped, with step-by-step physical instructions. Original goal is a lightweight, easy-to-use, always-on Linux machine with charging management. User has already been told about data loss, permanent Knox warranty-bit changes, and untested battery support in the documented k3gxx postmarketOS port. No further generic wipe confirmation needed, but do not flash unverified/incompatible software.

## Device verified via ADB

- Always target serial `0000000000000000`; another Android emulator is also connected and must not be modified.
- Model SM-G900H; product k3gxx; device k3g; armeabi-v7a, 2 GB RAM.
- Android 6.0.1 / SDK 23, security patch 2017-04-01.
- Build MMB29K.G900HXXU1CVHA; bootloader G900HXXU1CVHA; sales code THR.
- Kernel 3.10.9-7760371, build dated 2022-08-12.
- Knox warranty bit currently 0.
- Battery at initial read: 44%, charging by USB, 27.9 C, reports health 2. No physical battery inspection performed.
- /data size 11.6 GiB, 2.6 GiB free.
- Removable SD mounted /storage/F2F8-FC6A, 7.4 GiB total / 1.5 GiB free. User only authorized phone wipe; preserve SD contents unless explicitly needed and authorized.
- BOOT -> /dev/block/mmcblk0p9; RECOVERY -> /dev/block/mmcblk0p10; EFS -> /dev/block/mmcblk0p3. PIT downloaded read-only to stock-partitions.pit and parsed: BOOT ID9 is 13,631,488 bytes; RECOVERY ID10 is 15,728,640 bytes; USERDATA ID21 uses remaining space. TWRP fits recovery. Never write the PIT or repartition.
- Root binary not found. No phone partition writes, factory reset, rooting, or flashing performed.
- Google account was removed locally using the Accounts UI after the user unlocked the phone. Redacted account presence checks now report Google absent and Samsung absent. No cloud account was deleted. Phone PIN remains.
- `settings get` for lock_my_mobile, reactivation_lock_enabled, oem_unlock_allowed returned null. User inspected Download Mode and reported no REACTIVATION LOCK or FRP LOCK line shown; this is not a positive proof of unlocked state.

## Host and downloads

- macOS 26.5.1 arm64; ADB adb. Current permissions are danger-full-access, approval never: do not pass sandbox_permissions.
- Docker 27.4.0 linux aarch64. Running task container codex-s5-builder, image codex-s5-builder:local, Alpine 3.22, 4 CPUs and 4 GB memory. Privileged inside Docker VM for pmbootstrap mounts; no USB or credential mounts. Volume codex-s5-pmos-home mounted /home/pmos; source binds src/pmbootstrap read-only and src/pmaports read-write.
- Work folder: galaxy-s5-linux/work/galaxy-s5
- samloader 2.2.0 macOS universal downloaded from https://github.com/topjohnwu/samloader-rs/releases/tag/2.2.0 and its published asset digest verified. ZIP SHA256 74578787146ad284ad285b00c1270b7db0ff8d31576f19159103bb1bf56cf808. Extracted executable runs; --version and flash --help read successfully.
- TWRP twrp-3.7.0_9-0-k3g.img from official https://dl.twrp.me/k3g/twrp-3.7.0_9-0-k3g.img.html, 13,981,696 bytes. Published SHA256 verified: 1d121181fc65d0faf869ecfb2c49e9c9b4f0b343e558e1c0508a3e87c1c6929e. ANDROID! header verified. Saved image and .sha256 file locally.
- IMPORTANT: dl.twrp.me image downloads need Referer set to image .html download page. First no-referer response failed verification and was NOT saved or flashed. Correct referer returns binary and passes verification.
- samloader flash supports explicit --partition RECOVERY <file> --no-reboot. Do not use --erase/--pit/size bypasses. Download Mode detection and read-only PIT retrieval succeeded. No flash command run.

## Linux research and pending work

- Documented device: https://wiki.postmarketos.org/wiki/Samsung_Galaxy_S5_(International_3G)_(samsung-k3gxx). Search-indexed page reports screen/touch and USB networking work, charging/battery untested, audio partial; no verified Wi-Fi status. Native Linux remains experimental and always-on behavior must be tested before promising it.
- Wiki installation recipe: pmbootstrap install --android-recovery-zip --recovery-install-partition=data, export ZIP, install via TWRP from SD. Need inspect actual generated installer before use; do not destroy current OS until compatible image and recovery path ready.
- GitLab.com is STALE. Current official repo is https://gitlab.postmarketos.org/postmarketOS/pmaports, main branch. Device and kernel were archived with reason 'Kernel does not build anymore', commit 5ce8ad66ce7d2e3accfb10ca2f2a9a0c8b65bc70 (2025-08-17). No current prebuilt k3gxx packages located. User has been informed of the correction and experimental status.
- Linux APKBUILD pkgver 3.10.9, pkgrel 0, armv7, LineageOS/android_kernel_samsung_k3gxx commit 2f78dde45cd08d9f1f7f047c81ffb237f7ab9b56; adds dt.img via dtbTool-exynos. Local copies of README.md, pmaports.cfg, and kernel APKBUILD saved in this work folder with pmaports- filename prefix.
- Files with pmaports- prefix fetched from GitLab.com are stale. Build uses src/pmbootstrap commit edb3097c7307216b088478b7c424ee07d636f41b and src/pmaports f26bfce38e8bd1665ba4838b474d9d96f75d57f8. Locally moved device-samsung-k3gxx and linux-samsung-k3gxx from device/archived to device/downstream for experimental build.
- pmbootstrap 3.11.1 initialized in container: /home/pmos/.config/pmbootstrap_v3.cfg, work /home/pmos/work, aports /src/pmaports, device samsung-k3gxx, armv7, console UI, OpenRC (systemd requires a newer kernel), user user, hostname galaxy-s5, jobs4, extra packages openssh,iw,wpa_supplicant,wireless-tools,htop. No Linux password or SSH keys created yet.
- User explicitly requested Sol agents for hard tasks/code. /root/s5_kernel_build OWNS all build/container/pmaports mutations; do not run concurrent pmbootstrap commands. Agent fixed first GCC15 C23 failure by adding KCFLAGS=-std=gnu89; kernel objects now compiling. Original build exec session28004 has been handed to agent. Agent must report final build verification before rootfs work.
- /root/s5_power_wifi_review is an independent read-only Sol agent reviewing kernel battery/charging and Wi-Fi readiness. Do not treat source review as hardware verification.
- Stock nonpersonal Wi-Fi/BT firmware preserved in vendor-firmware/: bcmdhd_sta.bin, bcmdhd_apsta.bin, nvram_net.txt and two vendor variants, two BCM4350 .hcd files. No Wi-Fi credentials or personal files copied. Consider preserving BOOT/RECOVERY/EFS after TWRP permits reading. Never wipe bootloader/modem/EFS/calibration.
- Official Samsung check-update found exact installed THR firmware: G900HXXU1CVHA/G900HOJV1CVH1/G900HXXU1CPCA/G900HXXU1CVHA. Download completed to stock-firmware/SM-G900H_1_20221102135010_a29ifq45q7_fac.zip; full unzip -t passed for BL/AP/MODEM/CSC entries. Do not flash stock automatically.
- Following read-only PIT retrieval, another samloader print-pit session failed at protocol handshake, with no device write (download-mode-exit.log). User then held Power+Volume Down and returned to Android. ADB now reconnected; current battery55%, charging via USB,31.3C, reported health2. Start any future flash from fresh Download Mode, without an intervening --no-reboot PIT session.
- User confirmed SSH from this Mac, lightest option; no graphical desktop wanted. Dedicated Ed25519 key generated at ssh/s5_ed25519, public counterpart ssh/s5_ed25519.pub. Private key stays on Mac; never read/print it or copy/mount into build/image. Directory700/private600. No existing Mac keys used.
- A third requested Sol agent, /root/s5_install_review, is reviewing installer/rootfs compatibility read-only; no build or phone mutation permissions. Critical finding: recovery ZIP excludes ./home, installer recreates /home/user from /etc/skel; pmbootstrap ssh_keys flags only affect disk/image installs. Must stage public key at rootfs /etc/skel/.ssh/authorized_keys with700/600 before ZIP generation and verify resulting ZIP. Root locked; nonempty user password required; still checking doas administrative access. OpenRC sshd enabled by default. Installer partitions inside USERDATA into256MiB ext2 boot and remaining ext4 root, then writes BOOT. SYSTEM is untouched by installer and must eventually be formatted separately to satisfy full Android removal, after verifying native Linux works.
- Nothing flashed, rooted, formatted, or wiped. No native boot/hardware validation yet.

## Source review findings (not hardware validation)

- Kernel APK build succeeded after GNU89 flag and missing FIPS helper include/prototype fixes. APK /home/pmos/work/packages/edge/armv7/linux-samsung-k3gxx-3.10.9-r0.apk is7,214,096 bytes, SHA2563c7a42e22fcdf728db83901e09f4f59ec0385c98610a12f68928dcc6142d66d0. zImage7,281,488 bytes; DT114,688 bytes; total7,396,176 leaves6,235,312 bytes for ramdisk/header under BOOT limit. Kernel release3.10.9-LineageOS; DT samsung,K3G EUR,r04. Bootability untested.
- s5_kernel_build now owns ROOTFS/IMAGE construction as follow-up, not just kernel. May stage PUBLIC ssh key, install console/OpenRC packages, locked root/password SSH disabled and scoped passwordless doas for user. Must coordinate with install reviewer before final artifact; no phone commands. Main remains sole phone/flashing owner. Power agent doing independent kernel compatibility patch review and runtime checklist.
- Native kernel sec_battery.c runs charging protection autonomously: temperature, swelling, full/recharge, time limits, max77804 driver. Actual custom writable battery/store_mode=1 enables70%-stop/60%-resume; resets on probe/reboot, no write0 disable path. Generic charging_enabled is not exposed; generic current/voltage sysfs properties are read-only. Avoid batt_slate_mode and disabling protection. Store mode still needs phone tests. No evidence of batteryless bypass or boot-after-total-discharge support.
- Native Wi-Fi uses built-in BCMDHD/nl80211; defaults require /system/etc/wifi/bcmdhd_sta.bin and nvram_net.txt inside Linux rootfs before wlan0 up. This /system directory should NOT mount the physical Android SYSTEM partition. Preserve/check factory calibration and vendor variant. Wi-Fi is not yet hardware-validated.
- Mac bootstrap SOURCE path found: existing Android Samsung composite gadget supports ncm/acm/adb/ffs/rndis (no ecm); no kernel rebuild needed. Legacy sysfs /sys/class/android_usb/android0/{functions,enable} supports ncm, which creates ncm0. Native CDC NCM on Mac remains hardware untested. Install reviewer checking current initramfs legacy USB path and persistent OpenRC NCM setup. Builder instructed target172.16.42.1/24 and USB DHCP with no default gateway or DNS, if feasible. Never install old unsigned HoRNDIS host extensions.
- Mac network hardware-port baseline stored mac-network-before.txt. Existing interfaces include en0,en1,en2,en3,en4,en5,en7,en9,en12 etc.; discover NEW S5 NIC by before/after and USB descriptor. Do not change existing services/routes.
- Stock AP boot header extracted by install-review agent: Android v0,2048-byte pages, addresses match deviceinfo with base0x10000000, DTBH section. Current recovery installer nested MBR approach matches kernel loop/PARTSCAN/ext2/ext4 support. Final boot.img size still must be checked.
- Verified-downloads.sha256 records saved PIT,TWRP,and stock ZIP hashes. Stock ZIP full CRC verification passed.
- User reports battery flat/back cover fits normally. Last stock read62%,29.7C, USB charging; no new physical concern reported.
- Headless image refinement: console UI pulls NetworkManager/PipeWire/~350 packages. Builder now switching UI to none and rebuilding rootfs cleanly from local packages, retaining explicit SSH/network tools and OpenRC.
- Current initramfs legacy android_usb hardcodes rndis and helper lacks ncm0. Builder implementing small initramfs patch using existing valid deviceinfo_usb_network_function="ncm.usb0" (strip suffix for Android sysfs), plus ncm0 DHCP detection. Do NOT invent deviceinfo schema keys. OpenRC DHCP default elsewhere is172.16.41.1; explicitly use172.16.42.1/24 and disable obsolete usb0 service. Final artifact must verify effective NCM setup in initramfs/rootfs.
- Wi-Fi correction: first READ of /proc/deferred_initcalls executes built-in dhd initcall (not a write). Default dhd_download_fw_on_driverload=true, so firmware/NVRAM choice must precede that read. Builder informed; rootfs should start deferred driver after real root/proc/firmware exist. Driver supplies active MAC from chip/firmware; factory .mac.info used by Lineage macloader for vendor NVRAM selection.
- Tiny technical files to preserve after TWRP BEFORE wiping Data: /efs/wifi/.mac.info, /data/.cid.info (exists9bytes). /data/.ant.info absent. Stock SELinux denied reading .mac.info, nvram_path, and adb pulling .cid.info; no copy obtained yet. Do not copy Wi-Fi credentials/accounts. All NVRAM variants already preserved; exact selection before first driver start may require final ZIP regeneration after TWRP reads.
- Relevant macloader OUI mapping: wisol={48:5a:3f,70:2c:1f}; semco3rd={04:d6:aa,08:c5:e1,24:18:1d,2c:0e:3d,30:07:4d,54:88:0e,84:38:38,8c:f5:a3,ac:36:13,ac:5f:3e,b4:79:a7,c0:97:27,c0:bd:d1,c8:ba:94,d0:22:be,d0:25:44,e8:50:8b,ec:1f:72,ec:9b:f3,f0:25:b7,f4:09:d8,f8:04:2e}; else base nvram_net.txt. Never print full MAC unnecessarily. Prefer observed physical CIS label if it conflicts with old OUI mapping, then validate runtime.
- Install reviewer saved stock boot to galaxy-s5-linux/work/review-install/stock-boot.img (sibling of galaxy-s5 directory). Asked reviewer to extract stock recovery safely too. TWRP fstab matches exact actual partitions. Explicitly UNMOUNT Data in TWRP before installing ZIP; installer exact-path mount check may miss mounted symlink device.
- Independent SSH old-kernel review: CONFIG_SECCOMP_FILTER=y; OpenSSH10.3p1 has ARM ABI/fallback, no specific3.10 blocker found. User password hash must remain valid (do not lock named user); disable PasswordAuthentication AND KbdInteractiveAuthentication, require publickey, PermitRootLogin no, UsePAM yes, named-user passwordless doas. Verify effective sshd -T and final tar.
- Installer only replaces USERDATA and BOOT. After native boot/access/firmware validation, remove physical Android SYSTEM p18, CACHE p19, and HIDDEN/preload p20 to satisfy user full Android removal. Preserve bootloader/EFS/modem/TWRP/SD.

## Latest build and Wi-Fi migration checkpoint

- Headless rootfs --no-image build SUCCEEDED, UI none/OpenRC. Builder reports publickey-only SSH, doas, NCM/DHCP, deferred WiFi startup and firmware staged. PAM daemon is /usr/sbin/sshd.pam and /etc/conf.d/sshd explicitly selects it. Final ZIP/artifact audit still pending. Builder asked for current boot.img size/hash and optional modest Avahi daemon for galaxy-s5.local, plus exact pinning of locally patched boot-critical packages against accidental apk upgrade replacement.
- Ext4 compatibility gate PASSED: patched recovery installer1.0.7-r2 disables metadata_csum_seed and orphan_file in all THREE ext4 format branches (normal/encrypted/fallback boot). Exact armv7 e2fsprogs1.47.4 scratch256MiB formatting produced only has_journal,ext_attr,resize_inode,dir_index,filetype,extent,flex_bg,sparse_super,large_file,dir_nlink,extra_isize. All supported by3.10; no metadata_csum_seed/orphan_file/metadata_csum/huge_file/64bit. Scratch removed. This must still be audited in final ZIP.
- Stock restore images in sibling work/review-install/: stock-boot.img10,170,640 bytes SHA256457ae03b682c9a64cb1bd11616864d8ace7e8659a90789a7bb459b4e1e7a12b9; stock-recovery.img10,723,600 bytes SHA256d9644914d1fbe544c9afcf83163b90ed32084b7b048fdc3912a40da8e5a410f7.
- Bitwarden key names were listed with required value-redacting command; no WiFi credential key available. No Bitwarden secret values or notes were read/used.
- User explicitly chose REUSE SAVED PHONE WI-FI CONNECTION. s5_power_wifi_review now implementing Sol-coded helper in work/galaxy-s5/wifi-migration; no phone commands allowed for agent, root is sole phone owner. Helper should capture current stock SSID metadata before reboot; after TWRP read selected profile into Mac process RAM only and stage converted minimal config in phone TWRP tmpfs /tmp/codex-s5-wifi/config (700dir/600file). TWRP RAM holds it through data format. After ZIP install and before reboot, root mounts validated new native rootfs and commits staged profile to agreed wpa_supplicant path. NO WiFi secret values in Mac files, logs, chat, or exported image. Unsupported/ambiguous profiles fail closed and report only names/security metadata. Agent coordinates auto-start path with builder. Do not wipe before profile staged/verified.
- Root may proceed TWRP-ONLY once boot-image and filesystem gates pass, to read calibration/profile while final ZIP finishes. NO native install/USERDATA wipe until final complete artifact audit. Need helper current-SSID metadata command before reboot. No actual TWRP flash yet; Android remains intact and connected via ADB.

## TWRP flash checkpoint

- User connected preferred Wi-Fi; safe helper reports SSID HomeNetwork. No Wi-Fi secret read yet.
- Battery immediately before reboot78%,28.6C, USB charging. Exact model rechecked SM-G900H, TWRP SHA256 rechecked against official digest.
- ADB serial0000000000000000 reboot download completed; fresh IOUSB Gadget Serial04e8:685d and samloader detect succeeded.
- Ran samloader flash --partition RECOVERY twrp-3.7.0_9-0-k3g.img --no-reboot. Exit0, protocol/session/PIT/end messages, no errors. First partition write action attempted. TWRP boot/readback still must confirm effective result. No USERDATA or native BOOT writes.
- User given recovery button sequence and asked screen state; response pending.

## Verified TWRP and install hold

- User booted TWRP using battery removal then Volume Up+Home+Power. USB reconnected; exact serial0000000000000000 in recovery. ADB root uid0, /default.prop SM-G900H/k3g. TWRP UI version3.7.0_9-1_akhil1999 inside official image. Readback of first13,981,696 RECOVERY bytes matches official SHA256 exactly. TWRP successfully installed.
- Data mounted/decrypted as /dev/block/mmcblk0p21 at both /data and /sdcard. External SD /dev/block/mmcblk1p1 mounted /external_sd; preserve. /tmp is tmpfs~896MiB free. TWRP battery71%,30.8C,statusCharging.
- TWRP shell supports PTY only (shell -T fails); raw exec-out binary reads work. Wi-Fi stage safely failed before copying any profile because helper used shell -T. Power agent adapting raw exec-in transfer and tests; do NOT wipe until successful verified RAM stage.
- Mounted EFS p3 ext4 read-only,noload at /efs. Saved tiny factory/cid.info9bytes and factory/mac.info20bytes, directory700/files600. CIDsemco3rd and OUIc0:bd:d1 agree on nvram_net.txt_semco3rd variant. Builder informed to select variant while rebuilding. No whole EFS backup or writes.
- Independent final ZIP review caught boot header address mismatch: ramdisk0x21000000 vs stock0x11000000, tags0x20000100 vs stock0x10000100. Old deviceinfo used absolute values as offsets. First ZIP SHA256938e0d7821dc78a1d9096b6a0873fbd07d7afa5302113a5bedd4a4a865a12feb is REJECTED, DO NOT FLASH. Builder correcting offsets to0x01000000 and0x00000100, rebuilding device/initramfs/ZIP and fixing SSH tar ownership/modes/pin. Reviewer will re-audit. Native BOOT and USERDATA untouched.
- Installer review: successful install leaves mounted native root at /tmp/postmarketos/chroot/mnt/pmOS and boot at .../boot in outer TWRP namespace; backing expected /dev/mapper/mmcblk0p21p2 andp1 from kpartx USERDATAp21. Before configuration writes verify exact mount source/DM parent/labels pmOS_root andpmOS_boot, plus os-release. Do not blindly trust mapper filename.

- Hardware-path checkpoint: actual by-name symlinks rechecked in TWRP: BOOTp9,RECOVERYp10,SYSTEMp18,CACHEp19,HIDDENp20,USERDATAp21. Mac route to172.16.42.1 currently ordinary defaultvia192.168.1.1/en0; no existing specific conflicting route observed.
- Wi-Fi helper raw exec-in probe initially mismatched because host returns before remote write completes; later readback of synthetic29byte marker matched exactly. Agent adding bounded completion/content/mode verification and commit verification before deleting RAM stage. No real Wi-Fi profile transfer has occurred.
- Current TWRP battery75%,29.6C,Charging,~1.6GiB freeRAM, device clock27Sep2026 correct. FactoryCID need not be copied into native rootfs; compiled driver regenerates it. Select semco3rd NVRAM prior to deferred driver init.
- SSH host-key audit: no host keys in generated rootfs tar. OpenRC normally creates them on service start. Plan unique Ed25519 host key generation on real phone through verified TWRP-mounted native root/dev bind; pull PUBLIC .pub only, then strict dedicated known_hosts pin on Mac. No private host key leaves phone.

## Wi-Fi preserved; ready for final artifact gate

- Updated helper raw ADB transport uses unique completion markers and bounded polling. Actual synthetic probe PASSED, including byte/mode checks. Actual preferred HomeNetwork profile index6 then staged successfully in TWRP RAM at /tmp/codex-s5-wifi/config; hash readback and0600file/0700parent verified. No Wi-Fi secrets printed or written to Mac disk. DO NOT reboot recovery before commit unless first re-stage while Android Data still exists.
- Corrected rebuilt ZIP at task/pmos-samsung-k3gxx.zip currently hashes d13aae316a0155b9df839f4a0da48c0d632e523bd27357db4d3f32a954c9059d; final full independent audit pending. Builder reports embedded boot13,490,176 bytes SHA256dab39a4acfbeb07e0095445b1a2144e6a3ebd911567ebd9d45cf0ff40661699c, addresses nowmatchstock/TWRP.
- TWRP CLI nuance: twrp install returns0 regardless installer outcome. Require Installation done. in /tmp/postmarketos/pmos.log, no error flags, actual pmOS nested partitions/labels and exact BOOT readback match before reboot. No scripts print shadow/password contents according to source review.

## First native installer run stopped safely; recovery mount fix needed

- Final frozen a68 artifact independently cleared; phone stagedZIP62,686,303bytes full readback SHA256 a68c6c2242cc2eb9456574dc68a775edb404245009823aff5292fc094de4b022 matches host. Earlier d13 comparison failed because builder repacked pins before upload; no corruption found. Frozen a68 path artifacts/pmos-samsung-k3gxx-a68c6c22.zip mode0444.
- TWRP unmounted /external_sd and /data (including /sdcard alias), root unmounted read-only /efs. Actual /proc/mounts now only RAM/system virtual mounts + CACHEp19.
- Ran twrp install /tmp/pmos-samsung-k3gxx.zip. CLI exit0 but explicit error1. Log shows bootstrap extracted then stopped on outer pmos_chroot mount --bind: TWRP Android toolbox mount rejects long flag. NO BOOT/USERDATA write reached. Wi-Fi profile still in RAM.
- Root synthetic probe confirmed mount -o bind works correctly, and chroot /tmp/postmarketos/chroot /bin/busybox executes under TWRP kernel3.10.9-g69331c1c7-dirty. Builder will patch all3 outer mount --bind uses to -o bind and create NEW frozen artifact; reviewer validates. Old a68 artifact retained, DO NOT retry unmodified.
- Power agent prepared disabled charge-limit OpenRC service in power/s5-charge-limit; no native deployment until empirical baseline/store_mode validation. Launcher draft client/Connect S5.command awaits verified SSH/keypin and later copy to outputs.

## Native installation COMPLETE; first boot next

- Root created wrapper-only corrected immutable ZIP artifacts/pmos-samsung-k3gxx-a6773e32-twrp-bind.zip62,686,066bytes SHA256a6773e32ea0a90078b6e8e53511376c20dbca7a46279e9eb9569a4c4f035b70b. Independent differential audit PASSED: same60members, only3 mount --bind to -o bind replacements. Rootfs and BOOT bit-identical to approveda68. Phone upload hash verified using temporary bind + chroot BusyBox sha256sum.
- TWRP install retry SUCCESS: Installation done. logged, USERDATAp21 now nested MBR p1ext2labelpmOS_boot (2048..499711sectors),p2ext4labelpmOS_root (499712..24846335sectors). Both mapper sysfs slaves explicitly resolve physicalmmcblk0p21. Native root /tmp/postmarketos/chroot/mnt/pmOS os-releasepostmarketOSedge confirmed. BOOTp9 first13,490,176bytes SHA256dab39a4acfbeb07e0095445b1a2144e6a3ebd911567ebd9d45cf0ff40661699c EXACT match. Android DATA and old BOOT replaced; physicalSYSTEM/CACHE/HIDDEN still await removal after successful native access. SD/EFS/modem/bootloader/recovery preserved.
- Wi-Fi commit initially failed guard because os-release ID was quoted. Root mechanically fixed regex to accept balanced quotes, then actual commit PASSED with private hash/mode/owner validation. Selected HomeNetwork WPA-PSK now only in phone native/etc/wpa_supplicant/wpa_supplicant.conf root:root0600; RAM stage removed afterverification. Never printed credential or saved it to Mac.
- Native default NVRAM SHA256c0f069e5a0f1b9c856363d2c091645a1f112e0861e460861f117eb89f23ab3da EXACTmatchespreservedsemco3rdvariant. Home/user/.ssh ownerUID10000mode0700; authorized_keysUID10000mode0600. Installer chown onlyUID,group0but privatepermissions correct.
- Unique Ed25519 SSH HOST key generated on PHONE in nativechroot with/devbind, privatekeyneverpulled. Public only saved ssh/s5_host_ed25519.pub. Dedicated ssh/known_hosts600 uses aliascodex-galaxy-s5. FingerprintSHA256:BBw52ZqR/mGWFWXCLkEu/CQLiDhpwFb9a9Jly/xc7+o. Clientprivatekey ssh/s5_ed25519 staysMac600.
- Allnative/boot andbootstrapbindmounts synced and unmounted successfully. Only TWRPvirtualmounts+physicalCACHEp19remain. Fresh Mac interface listbaseline mac-interfaces-before-native.txt. First native reboot next; no native runtime verification yet.
- Power source finding: charger-triggered boot may startLinux because initramfs ignoresandroidboot.mode=charger, but lpcharge1 DISABLES70/60storemodebranch despite store_mode readback1. Probe /proc/cmdline andbattery/batt_lp_charging (mayemptywhen0). No supported runtimeclear found; do NOT inventwrites. Normal firstrebootshouldbe0. Testcleanpoweroff/replug laterhealthySOC, nointentionaldeepdrain.

## First native boot failed: loop; recovery return pending

- ADB normal reboot issued after allfilesystems synced/unmounted. Afterminutes noSamsung/LinuxUSB, no newMacNIC, no galaxy-s5.local mDNS, factoryMACnotinMacARPcache.
- User reports repeating Samsunglogo with kernel is not seandroid enforcing. Nativeboot/SSH/charging NOTworkingorverified. Asked warmPower+VolDown thenUp+Home+Power withoutbatteryremoval to preserveRAMlogs. User didfirststep and reportsbattery-emptygraphic+kernelwarning, stuckwhileUSBconnected. Lastmeasuredbattery83%,29C; graphicmaycharger-mode,notestablishedemptybattery.
- Latestuserinstruction: UNPLUGUSB, keepbatteryin, holdVolumeUp+Home+Power up15sec; releasePoweratlogo, keepUp+HomeuntilTWRP; reconnectUSBwhenopen. Asyncscreenquestionpending. NoUSB/ADBcurrently.
- Kernel andinstallreviewagents interruptedbyuserturnabort, thenreactivated toinvestigateactualuncompressedkernelmemoryextent/PHYS_OFFSET, DT/bootheadersemantics,originalportoffsethistory. Do notassumeheaderstockmatchingrequiredorRAMdiskoverlapwithoutphysicalDRAMevidence; Exynosbootloadermayignoreheaderaddresses. NeedwarmTWRPlast_kmsg/pstorecapturebeforemorekernelchanges.

## Concrete firstboot blocker identified; BOOT-only fix pending

- Warm buttonattempt failed; batteryreset returnedTWRP. USBexactserialconnectedroot; battery78% (emptygraphicwasnotactualempty). /data notmounted (expectednestedLinuxMBR); externalSDautoremountedbyTWRP,preserve.
- Saved sanitized diagnostics/first-native-last-kmsg.txt from/proc/last_kmsg. This iscurrentrecoverySBOOTlog, notnativekernelpanic, butitidentifiesactualhardware/chipandactualbootloadaddresses.
- Actual chip0x152e, platform0x1e92, subtype0x7d64f612, hwrev0x0a. TWRP/stockDTBH hasmatchingrev10..255entry. Actualkernelplaced0x40008000, initrd0x42000000,DT0x41f00000. DTS/PHYS_OFFSET DRAMstarts0x40000000. Thusnominalbootheaderaddressesareignored/reinterpreted; changingstockmatchingheaderonoverlaparithmeticwouldbeunjustified.
- ROOT CAUSE CANDIDATE withstrongindependentconfirmation: nativeDTBH uses WRONG dtbTool defaults platform0x50a6/subtype0x217584da. Nativechip0x152e/rev10..255andactualDTBpayloadDOmatchthisphone. EmbeddedFDTmodel_info-platform k3g,subtypek3g_eur_open,revision10sameasstockrev10; onlywrappernumericmetadatawrong. NativeAPKBUILD omittedexplicit--platform/--subtype.
- KernelagentpreparingminimalBOOT-onlycorrectedimagefromexistingzImage/ramdisk/DTB, changingDTBHIDs to0x1e92/0x7d64f612andcorrectimagechecksumifneeded. Installreviewagentindependentlyreviews. NOreinstall/wipeUSERDATA; installedWiFi/key/rootfsretained. Need laterupdate/boot/dt.imgand/boot/boot.imgsofutureinitramfsupdateskeepfix, plusdurablesourcefix.
- Footercheck: stockboothasSEANDROIDENFORCE272bytesfromend; BOTH officialworkingTWRPandnativebootlackit. Warningtextaloneisnotproofoffailure. Do notchangefooterasblindfix.

## Native CPU-idle panic identified; source fix building

- Corrected DTBH-only BOOT cd25de6c65609309f9e93fb4ec2564304ee752eac76704d9080b3d8cc8ae4d05 was full readback-verified after BOOT-only flash. Native still rebooted. User successfully caught Volume Up+Home+Power during Samsung loop without battery removal, so TWRP preserved clean native panic in diagnostics/second-native-last-kmsg.txt.
- Native log proves hardware rev10 selected, Linux3.10.9-LineageOS GCC15.2.0 begins boot/initramfs unpack, watchdog inactive. At2.318s CPU5 faults ffffffe4 in __cpu_suspend_save+0x64 with r11=0, then sec_debug hardware reset after panic.
- Kernel and independent review agents identified concrete callee-save ABI violation: arch/arm/mm/proc-v7.S cpu_v7_do_suspend saves r4-r10 but overwrites r11. GCC15 frame-pointer caller subsequently executes ldr r1,[r11,#-28]. Minimal fix preserves/restores r11 as well; no intervening BL so temporary4-byte stack alignment does not cross call boundary. Leave CP15 context size and restore data unchanged.
- Builder authorized kernel rebuild + corrected DTBH + unchanged native ramdisk, BOOT-only artifact; independent review required. Root exclusively controls ADB/flashing; no USERDATA reinstall. Physical phone native rootfs/WiFi/host key preserved.
- Current recovery ADB exact serial connected uid0; battery84%,30.2C,Charging. TWRP auto-mounts CACHE and external SD only; SD must remain untouched. Native filesystem not currently mounted.
- TWRP-kernel fallback feasibility reviewed, not prepared: known-working kernel + current native ramdisk + preserved5-DTB trailer projects13,514,752bytes (116,736bytes under BOOT limit), kernel binary has devtmpfs/loop/DM/ext4/NCM/DHD, but no embedded config; fallback remains untested.

- r1 kernel build and independent BOOT gate PASS. Frozen artifacts/boot-k3gxx-r11-47e72417.img13,490,176bytes SHA25647e7241722b5d87508be4f25f796acb338a7bc4f6dd77ebaf9da70674eed660b; zImage+8bytes, identical ramdisk and correctedDT, correct Android ID550905aa1f0355925f6ed4d9cdc9d6d683eeb3cd. Phone RAM copy fullhash verified. BOOT-only dd completed; rawcat full13,631,488partition readback verifies image prefix exact47e724 hash. Initial dd readback included98byte transferstats in stdout; corrected using cat, no corruption. ExternalSD unmounted; onlyCACHE remained. Native reboot now issued. Battery90%,29.1C beforeflash. Native runtime pending.

## r11 native boot reaches USB network; Mac Local Network permission check pending

- After r11 reboot, Mac stable new en13 NCM interface and SAMSUNG_Android USB IOobject0x100a2b9b9. DHCPACK correct client172.16.42.2/24, server172.16.42.1, NOgateway/DNS. Valid route172.16.42.0/24 viaen13. USBobjectunchangedminutes; user reports blackscreen, NOTlogo loop. NativeSSH/fullrootfs still unverified.
- SSH/ping172.16.42.1 fails No route tohost; ARPincomplete. Crucial independent comparison: knownMacrouter192.168.1.1 ALSO immediateping No route tohost despitevaliddefaulten0routeandresolvedARP. ThusMacLocalNetworkprivacypermission likelymajorconfounder, NOTevidence tochangephoneagain. TargetedMaclogs associateLocalNetworkevents withcom.openai.codex. RequesteduserSystemSettings > Privacy&Security > LocalNetwork > enableCodex. Awaitresponse. DoNOTreflash/recoverphonewhilecheckingMacpermission.
- Agents audited actualr11ramdisk andinstalledZIP: HOST_IP172.16.42.1, ifconfig ncm0 assignedbeforeDHCP; nativeOpenRCs5-usb-ncm+unudhcpd.ncm0boot,sshddefaultenabled. NoIP/scriptmismatch.
- Packetcapture unavailablewithoutMacadminBPFaccess; sudo-nrequirespassword, noescalationperformed. NoMacrouting/servicechangesmade. TerminalCLI canhaveotherprivacypermissions; doNOTsilentlybypassCodexprivacydenial. Userpermissiontoggleispreferred.
- r1APKnowexported artifacts/linux-samsung-k3gxx-3.10.9-r1.apk SHA256bd44b65fd8e4c05668f801ee34e3841491bb691461ac964ad33b4499681df7ea. Laternativeupgrade dryrunbuilderprovenreplaceexistingr0worldpinonepackageonly, noallow-untrusted. mkinitfstriggerregenerates/boot/boot.img; currentdeviceinfodoesNOTenableflash_kernel_on_update, so noautomaticphysicalBOOTflash. Recheckinstalledflag/sizebeforenativeupgrade.
- RecoverymaintenanceREADME prepared bypoweragent at recovery-maintenance/README.md, staticsh-nchecks only, unexecuted. Supports mountingnestednativevolumesfromfreshTWRPwithoutinstaller/wipe.

## User requested retest; macOS privacy denial now CONCLUSIVE

- User said test again; SSH S5 andpingrouter stillimmediate No route tohost. PhoneUSBsameIOid and en13DHCPactive, no phonechanges.
- Sol powerreviewagent ran bounded Network.framework Swiftprobe under SAME Codex responsibleapp. Both172.16.42.1:22/en13 and192.168.1.1:80/en0 explicitly return NWPath.Status.unsatisfied, NWPath.UnsatisfiedReason.localNetworkDenied. This conclusivelyidentifiesMacLocalNetworkprivacydenial, no guessingfromARPnow. NoTCC/VPN/routechanges or Terminal/Dockerbypass.
- Nextuserstep: ensureCodexLocalNetworktoggleON; ifalreadyON, fullyquit/reopenCodex thenresume/retry. Relauchingispragmaticrefresh, notApple-documentedmandatoryrestart. DoNOTreboot/flashphone tofixMacpermission. Currentphoneblackheadlessscreen, nativeUSBactive, SSH/fullrootfs/powerstillawaitvalidation. Work/source/artifacts safelysaved.

## User Terminal test also fails; warm recovery requested

- User cannot close/reopen Codex and requested a Terminal command. Provided key-pinned read-only SSH uname/uptime probe using existing local identity and known_hosts; user reports No route to host from that test too. Therefore Codex privacy denial is real but is not a sufficient explanation of all connection failure; do not assume native rootfs finished boot.
- Current read-only check: same Samsung USB IOobject, en13 active .2/24 with original DHCPACK; .1 ARP incomplete; no ADB. No new phone/image/network writes.
- Asked user to leave USB connected and hold VolumeUp+Home+Power, release Power at Samsung logo and retain other two until TWRP. Goal warm last_kmsg and rootfs startup-log collection. Reply pending; exact-serial ADB not yet present.
- Kernel agent awaiting new native log; install reviewer locating exact initramfs/OpenRC log paths; power agent writing a bounded, sanitizing /proc/last_kmsg capture helper. Root remains sole phone operator. No speculative rebuild or reinstall.

## Recovered TWRP; no persistent native boot log; diagnostic logging being prepared

- User battery-reset into TWRP and said proceed. ADB exactserial recovery uid0, TWRPkernel3.10.9-g69331c1c7-dirty, battery93%Charging. Current /proc/last_kmsg captured by diagnostics/capture_last_kmsg.py to third-native-last-kmsg.txt (310lines46redacted); onlySBOOT, batteryreset erasednativeRAMlog.
- Verified a677ZIP hostSHA andfullphoneRAMcopySHA. Staged onlybootstrapBusyBox/kpartx/libs fromZIP under/tmp/codex-s5-maint/chroot; bind-mounted/dev,/proc,/sys there. Neverraninstaller. ExternalSDunmounted. kpartx readmatchedexactMBR then createdRAMmaps.
- MaintenanceREADMEinitial check_map stopped becausekpartxcreatedREALblocknodes notsymlinks. Independentactualchecks: /dev/mapper/mmcblk0p21p1 major254:0 ↔ dm-0,namep1,size497664; p2major254:1 ↔ dm-1,namep2,size24346624; bothslavephysicalmmcblk0p21. PoweragentfixedREADMEguard usingblockmajor:minor/sys/dev/block. DoNOTrerunmappingwhileexistingmapsremain.
- BothvolumesnowmountedREADONLY: rootext4 ro,noload at/tmp/codex-s5-native, bootext2 ro at.../boot. Labels pmOS_root/pmOS_boot correct; OSpostmarketOSedge. /var/log containsONLYapk.log16350bytesfrominstall. No/var/log/dmesg,rc.log,logbookd.db,messages, so no bootmisc evidence. /sys/fs/pstore absent. Root/sbin/init→/bin/busybox correct; nativeBusyBox/ld-musl executable; chrootnativeBusyBoxtrueworksunderTWRP. StandardOpenRCinittab.
- PhysicalCACHEp19 confirmed409600sectors=209715200bytes, ext4UUID57f8f4bc-abf4-655f-bf67-946fc0f9f25b. Cachecurrentlymounted/cachebyTWRP asusual.
- Builder now preparing minimalDIAGNOSTICramdiskBOOT patch torecordearlyinitramfs/kernel logs persistently onthisexactCACHEwithoutformatting, preservingr1kernel/correctedDT/rootfs. Reviewerindependentlycheckingguards andmount/loggercleanupat switch_root. Nocandidateyet,noreflashafterr1147e724, nocauseguessed. GoalgetactualstartupfailureevidencewithoutnetworkorwarmRAMdependency.
- Before nextboot MUSTsync/unmountnativeROroot/boot,bootstrapbinds,detachp21DMmapsviaauditedcleanup. Currentmountsremainforinspection. DoNOTwipeSYSTEM/CACHE/HIDDENuntilnativeworks; diagnosticCACHEneededtemporarily. WiFiprofile/SSHkeysunchangedoninstalledrootfs.
- Native MMC driver source DOES enable GENHD_FL_EXT_DEVT, so CONFIG_MMC_BLOCK_MINORS=8 does not limit physical p19/p21. CACHE rawsuperblockfeatures compat0x0c,incompat0x46,ro_compat0x13,block4096; legacy3.10compatible; by-nameCACHE→p19,start5406720,size409600,189.8MBfree.
- TemporaryOpenRCdiagnosticlogging now ENABLED on installedroot: original/etc/rc.conf saved withcp-p at/etc/s5-boot-diagnostics/rc.conf.original inside0700dir; appendedrc_logger="YES" andrc_log_path="/var/log/rc.log"; nativeBusyBoxsh-n andreadbackpassed, var/logwritable, synced. No service/inittab/packagechanges. Root+bootunmounted/remountedREADONLYagainafteredit. MUSTrestoreoriginalrc.confafterdiagnosis.
- BuilderverifiedactualramdiskblkidUUIDflags, BusyBoxtaillimits/mktemp/unmount; implementingCACHElogger. CandidateNOTyetready; physicalBOOTstill47e724. Loggerplanbounded20sdevicewait, p19identityguards, boundedkernel/initrdlog snapshots2sinterval+sync, gracefulboundedstop/switchrootmarker/unmount beforehandoff. Reviewerwillgateartifact.

## r17 — USB serial: the function must be created before the bind (2026-09-27)

Read the r16 panel log and found the cause, which was not a mystery:

- `setup_usb_network_configfs` cannot create `ncm`/`rndis` (absent from this
  kernel) but still writes the UDC, so it binds a *functionless*
  configuration. The kernel then refuses to add a function to it: `EBUSY` on
  the mkdir, `ENOENT` on the symlink, `ENXIO` on the unbind. r16 created
  `acm.usb0` from `mount_root_partition`, long after that bind, so every step
  failed.
- r16's success test was also wrong. It looked for `/dev/ttyGS0`, which the acm
  driver creates on load whether or not a function was linked, so it printed
  "USB serial gadget is up" on the very boot where the host enumerated nothing.

Fix: new region `s5screen: usb serial function`, a **pure insertion** into
`setup_usb_network_configfs` immediately before the shipped
`setup_usb_configfs_udc`, creating and linking `acm.usb0` while the
configuration is still unbound. Stripping all 11 regions reproduces the r11 base
byte-exactly. `ensure_usb_serial`'s gadget half became a report that tests the
link in `configs/c.1`, never `/dev/ttyGS0`; the getty half is unchanged.

Built `boot-k3gxx-r17-acmearly-3b80249b.img`, 13,348,864 B, md5
`221aa0ccd1abdb602a6ff51412937e3e`, sha256 `3b80249b…`, 282,624 B spare.
Verifier: 96 checks, 0 FAIL, 0 SKIP; still rejects the r16 and r15 images.

New tests: `diagnostics/mock_usb_gadget_r17.sh` (runs the real shipped function
against a mock configfs enforcing the kernel rule),
`diagnostics/test-usb-serial-r17.sh` (5 cases),
`artifacts/source/negatives-verify-r17.py` (9 cases, each rejected by its named
check). Two real bugs surfaced while writing them: the new region's `tr` was
double-escaped so it would have eaten every `n` in the listing, and the verifier
still loaded `repack-boot-r15.py`, so it would have checked a 10-entry REGIONS
list.

Also fixed three stale `work-docs/...` paths in this file
(line 25, line 64) and in `recovery-maintenance/README.md` line 8.

Next: reboot to TWRP, flash r17 to p9, verify md5 on the phone, then boot.
