#!/bin/sh
# s5-wifi - Interactive and CLI Wi-Fi management tool for Samsung Galaxy S5
# Supports scanning, listing saved networks, connecting with masked password,
# and switching between multiple saved networks.

set -eu

CONF="/etc/wpa_supplicant/wpa_supplicant.conf"
WPA_CLI="/usr/sbin/wpa_cli -i wlan0"

die() {
    echo "ERROR: $*" >&2
    exit 1
}

check_root() {
    if [ "$(id -u)" -ne 0 ]; then
        die "This command requires root. Run with: sudo s5-wifi $*"
    fi
}

get_current_ssid() {
    $WPA_CLI status 2>/dev/null | grep -E "^ssid=" | cut -d'=' -f2 || true
}

get_current_ip() {
    ip -4 addr show wlan0 2>/dev/null | grep -oE "inet [0-9.]+" | cut -d' ' -f2 || echo "None"
}

cmd_status() {
    echo "=== Galaxy S5 Wi-Fi Status ==="
    state=$($WPA_CLI status 2>/dev/null | grep -E "^wpa_state=" | cut -d'=' -f2 || echo "DISCONNECTED")
    ssid=$(get_current_ssid)
    ip=$(get_current_ip)
    
    echo "State:       $state"
    echo "SSID:        ${ssid:-[None]}"
    echo "IP Address:  $ip"
    
    if [ -n "$ssid" ]; then
        bssid=$($WPA_CLI status 2>/dev/null | grep -E "^bssid=" | cut -d'=' -f2 || echo "N/A")
        freq=$($WPA_CLI status 2>/dev/null | grep -E "^freq=" | cut -d'=' -f2 || echo "N/A")
        echo "BSSID:       $bssid ($freq MHz)"
    fi
}

cmd_scan() {
    check_root
    printf "Scanning Wi-Fi channels (sweeping 2.4GHz & 5GHz)..."
    $WPA_CLI scan >/dev/null 2>&1 || true
    
    # Broadcom BCM4354 takes ~3.5 - 4.5 seconds to scan all 2.4GHz + 5GHz bands
    for i in 1 2 3 4 5; do
        sleep 1
        printf "."
        count=$($WPA_CLI scan_results 2>/dev/null | grep -c "^[0-9a-f]" || true)
        if [ "$count" -gt 3 ] && [ "$i" -ge 3 ]; then
            break
        fi
    done
    echo " Done!"
    echo ""
    printf "%-30s %-10s %-12s\n" "SSID" "SIGNAL" "SECURITY"
    printf "%-30s %-10s %-12s\n" "------------------------------" "----------" "------------"
    $WPA_CLI scan_results 2>/dev/null | awk -F'\t' 'NR>1 && $5 != "" {
        ssid = $5
        sig = $3 " dBm"
        sec = ($4 ~ /WPA2|SAE|WPA/ ? "WPA/WPA2/WPA3" : "Open")
        printf "%-30s %-10s %-12s\n", ssid, sig, sec
    }' | sort -u
}

cmd_list() {
    check_root
    echo "=== Saved Wi-Fi Networks ==="
    $WPA_CLI list_networks 2>/dev/null || echo "wpa_cli could not list networks"
}

cmd_switch() {
    check_root
    target="${1:-}"
    if [ -z "$target" ]; then
        echo "Usage: sudo s5-wifi switch <network-id-or-ssid>"
        cmd_list
        exit 1
    fi

    # Check if target is a numeric ID
    net_id=""
    if echo "$target" | grep -qE "^[0-9]+$"; then
        net_id="$target"
    else
        # Find ID by SSID
        net_id=$($WPA_CLI list_networks 2>/dev/null | awk -v s="$target" '$2 == s { print $1; exit }')
    fi

    if [ -z "$net_id" ]; then
        die "Network '$target' not found in saved networks. Run 'sudo s5-wifi list'."
    fi

    echo "Switching to network ID $net_id..."
    $WPA_CLI select_network "$net_id" >/dev/null
    $WPA_CLI enable_network "$net_id" >/dev/null

    echo "Waiting for association..."
    for i in $(seq 1 15); do
        state=$($WPA_CLI status 2>/dev/null | grep -E "^wpa_state=" | cut -d'=' -f2 || true)
        if [ "$state" = "COMPLETED" ]; then
            break
        fi
        sleep 1
    done

    echo "Renewing DHCP lease..."
    udhcpc -i wlan0 -q -n -s /usr/share/udhcpc/default.script >/dev/null 2>&1 || true
    sleep 2
    cmd_status
}

cmd_connect() {
    check_root
    target_ssid="${1:-}"
    if [ -z "$target_ssid" ]; then
        printf "Enter Wi-Fi SSID to connect to: "
        IFS= read -r target_ssid
    fi

    [ -z "$target_ssid" ] && die "SSID cannot be empty"

    printf "Enter password for '%s' (input hidden): " "$target_ssid"
    stty -echo 2>/dev/null || true
    IFS= read -r target_psk
    stty echo 2>/dev/null || true
    printf "\n"

    # Add network via wpa_cli
    net_id=$($WPA_CLI add_network 2>/dev/null | tail -n 1)
    if [ -z "$net_id" ] || ! echo "$net_id" | grep -qE "^[0-9]+$"; then
        die "Failed to allocate new network entry in wpa_supplicant"
    fi

    echo "Configuring network ID $net_id for '$target_ssid'..."
    $WPA_CLI set_network "$net_id" ssid "\"$target_ssid\"" >/dev/null
    
    if [ -n "$target_psk" ]; then
        $WPA_CLI set_network "$net_id" psk "\"$target_psk\"" >/dev/null
    else
        $WPA_CLI set_network "$net_id" key_mgmt NONE >/dev/null
    fi
    unset target_psk

    echo "Selecting and enabling network..."
    $WPA_CLI select_network "$net_id" >/dev/null
    $WPA_CLI enable_network "$net_id" >/dev/null

    echo "Waiting for association (up to 20s)..."
    associated=0
    for i in $(seq 1 20); do
        state=$($WPA_CLI status 2>/dev/null | grep -E "^wpa_state=" | cut -d'=' -f2 || true)
        if [ "$state" = "COMPLETED" ]; then
            associated=1
            break
        fi
        sleep 1
    done

    if [ "$associated" -eq 1 ]; then
        echo "Connected! Requesting IP address..."
        udhcpc -i wlan0 -q -n -s /usr/share/udhcpc/default.script >/dev/null 2>&1 || true
        sleep 2
        
        # Save configuration
        $WPA_CLI save_config >/dev/null 2>&1 || true
        echo "Configuration saved."
        cmd_status
    else
        echo "Association failed or timed out. Reverting to previous network..."
        $WPA_CLI remove_network "$net_id" >/dev/null 2>&1 || true
        cmd_status
        exit 1
    fi
}

cmd_help() {
    echo "Galaxy S5 Wi-Fi Manager (s5-wifi)"
    echo "Usage:"
    echo "  s5-wifi [status]         - Show current Wi-Fi connection and IP"
    echo "  sudo s5-wifi scan        - Scan for available Wi-Fi networks"
    echo "  sudo s5-wifi list        - List all saved networks"
    echo "  sudo s5-wifi switch <id> - Switch to a saved network (by ID or SSID)"
    echo "  sudo s5-wifi connect [S] - Connect to a new Wi-Fi network (prompts for password)"
}

case "${1:-status}" in
    status)  cmd_status ;;
    scan)    cmd_scan ;;
    list)    cmd_list ;;
    switch)  shift; cmd_switch "$@" ;;
    connect) shift; cmd_connect "$@" ;;
    help|-h|--help) cmd_help ;;
    *)       cmd_help ;;
esac
