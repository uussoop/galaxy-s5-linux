#!/bin/ash
# Replace the saved Wi-Fi PSK, then associate. The key is read from the terminal
# with echo disabled and is never written to stdout, never passed as a command
# argument, and never leaves the phone. That last part matters: passing it as an
# argument would put it in /proc/*/cmdline, and a shell loop keeps it in the
# process's own memory instead.
#
# The rewrite walks the file line by line rather than using sed or awk -v,
# because the password can contain a backslash, an ampersand or a pipe, and
# every one of those is a metacharacter to sed. An awk -v assignment would
# additionally eat backslashes, quietly corrupting a perfectly good password.
set -u

conf=/etc/wpa_supplicant/wpa_supplicant.conf
bak="$conf.s5bak"

if [ ! -s "$conf" ]; then
    echo "no profile at $conf" >&2
    exit 1
fi

printf 'Wi-Fi password: '
stty -echo 2>/dev/null || true
IFS= read -r psk
stty echo 2>/dev/null || true
printf '\n'

if [ -z "$psk" ]; then
    echo "empty password given, nothing changed" >&2
    exit 1
fi

cp -p "$conf" "$bak" || exit 1

# Replace only the first psk= line and leave everything else byte-identical.
# The value is written by the shell, so quoting inside it is harmless.
done=no
while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
        *psk=*)
            if [ "$done" = no ]; then
                printf '\t\tpsk="%s"\n' "$psk"
                done=yes
            fi
            ;;
        *)
            printf '%s\n' "$line"
            ;;
    esac
done < "$bak" > "$conf.tmp" || exit 1

if [ "$done" = no ]; then
    echo "no psk= line found, profile left alone" >&2
    rm -f "$conf.tmp"
    exit 1
fi

# The new key is written to a separate file and moved into place, so an
# interrupted run cannot leave a half-written profile behind. The old key still
# exists in $bak and possibly in unallocated blocks of the flash; that is
# honest and is why $bak is kept rather than deleted.
mv -f "$conf.tmp" "$conf" || {
    echo "could not move the new profile into place; original is intact" >&2
    rm -f "$conf.tmp"
    exit 1
}
chmod 600 "$conf"
unset psk

echo "key updated, old profile kept at $bak"
echo "restarting wpa_supplicant"

pkill -9 wpa_supplicant 2>/dev/null || true
sleep 2
rm -rf /var/run/wpa_supplicant
ip link set wlan0 down 2>/dev/null || true
sleep 3
ip link set wlan0 up 2>/dev/null || true
sleep 2

wpa_supplicant -B -P /run/s5-wpa.pid -i wlan0 -D nl80211 -c "$conf" || {
    echo "wpa_supplicant failed to start" >&2
    exit 1
}
sleep 8
wpa_cli -i wlan0 select_network 0 >/dev/null
wpa_cli -i wlan0 enable_network 0 >/dev/null
sleep 18

state=$(wpa_cli -i wlan0 status 2>/dev/null | sed -n 's/^wpa_state=//p')
echo "wpa_state=$state"

if [ "$state" = COMPLETED ]; then
    echo "associated, requesting an address"
    udhcpc -b -i wlan0 -p /run/s5-udhcpc.pid \
        -s /usr/share/udhcpc/default.script
    sleep 6
    ip -4 addr show wlan0 | grep inet || echo "associated but still no IPv4"
else
    echo "not associated yet; leaving wpa_supplicant running to retry"
fi
