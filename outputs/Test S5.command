#!/bin/bash
# Manual, read-only SSH check for the Galaxy S5.
#
# Requires the Mac to be on the SAME LAN as the phone (192.168.1.0/24).
# The phone's address comes from DHCP and can move; override it with
#   S5_HOST=192.168.1.155 ./Test\ S5.command
set -u
base="$(cd "$(dirname "$0")/.." && pwd)"
hosts="$base/work/galaxy-s5/ssh/known_hosts"
log="$base/work/galaxy-s5/diagnostics/mac-terminal-ssh-test.txt"
host="${S5_HOST:-192.168.1.100}"
user="${S5_USER:-user}"
tries="${S5_TRIES:-3}"
# Prefer the unencrypted agent key, which needs no passphrase. Fall back to the
# user's own key, which is passphrase-protected and only works from a terminal
# where a human can type it.
agentkey="$base/work/galaxy-s5/ssh/s5_agent_ed25519"
if [ -n "${S5_KEY:-}" ]; then
  key="$S5_KEY"
elif [ -r "$agentkey" ]; then
  key="$agentkey"
else
  key="$HOME/.ssh/id_ed25519"
fi
umask 077
: > "$log" || exit 1
display_key="${key#$base/}"
printf 'Galaxy S5 SSH check: %s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" | tee -a "$log"
printf 'target %s@%s  key %s\n' "$user" "$host" "$display_key" | tee -a "$log"

if [ ! -r "$key" ]; then
  printf 'FAILED: no readable private key at %s\n' "$display_key" | tee -a "$log"
  exit 2
fi

result=255
try=1
while [ "$try" -le "$tries" ]; do
  printf -- '--- attempt %s of %s ---\n' "$try" "$tries" | tee -a "$log"
  /usr/bin/ssh -F /dev/null -i "$key" \
    -o UserKnownHostsFile="$hosts" \
    -o IdentitiesOnly=yes -o BatchMode=yes \
    -o StrictHostKeyChecking=yes -o HostKeyAlias=codex-galaxy-s5 \
    -o ConnectTimeout=8 "$user@$host" 'uname -r; uptime; id' 2>&1 | tee -a "$log"
  result=${PIPESTATUS[0]}
  [ "$result" -eq 0 ] && break
  try=$((try + 1))
  [ "$try" -le "$tries" ] && sleep 3
done

if [ "$result" -eq 0 ]; then
  message='SUCCESS: Read-only SSH check completed.'
else
  message="FAILED: SSH exited with status $result."
  case "$result" in
    255) message="$message  (no route / no answer - is this Mac on 192.168.1.0/24?)" ;;
    254) message="$message  (private key rejected or unreadable)" ;;
    *)   message="$message  (SSH reached the phone, auth or command failed)" ;;
  esac
fi
printf '%s\n' "$message" | tee -a "$log"
printf 'Result saved to %s\n' "${log#$base/}"
if [ -t 0 ]; then read -r -p 'Press Enter to close this window... ' _; fi
exit "$result"
