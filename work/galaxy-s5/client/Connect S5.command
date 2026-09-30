#!/bin/sh
# Copy this launcher into the project's outputs directory before use.
set -eu

if [ "$#" -gt 1 ]; then
    printf 'Usage: %s [host]\n' "$(basename "$0")" >&2
    exit 2
fi

host=${1:-galaxy-s5.local}
case $host in
    ''|-*|*[!A-Za-z0-9._:-]*)
        printf 'Invalid host. Use a DNS name or numeric address.\n' >&2
        exit 2
        ;;
esac

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
project_root=$(dirname "$script_dir")
ssh_dir=$project_root/work/galaxy-s5/ssh
key=$ssh_dir/s5_ed25519
known_hosts=$ssh_dir/known_hosts

if [ ! -f "$key" ]; then
    printf 'S5 SSH identity is missing: %s\n' "$key" >&2
    exit 1
fi
if [ ! -s "$known_hosts" ]; then
    printf 'S5 pinned host key file is missing or empty: %s\n' "$known_hosts" >&2
    exit 1
fi
if ! ssh-keygen -F codex-galaxy-s5 -f "$known_hosts" >/dev/null 2>&1; then
    printf 'S5 host key is not pinned as codex-galaxy-s5 in %s\n' "$known_hosts" >&2
    exit 1
fi

exec ssh -F /dev/null \
    -i "$key" \
    -l user \
    -o "UserKnownHostsFile=$known_hosts" \
    -o GlobalKnownHostsFile=/dev/null \
    -o HostKeyAlias=codex-galaxy-s5 \
    -o StrictHostKeyChecking=yes \
    -o UpdateHostKeys=no \
    -o VerifyHostKeyDNS=no \
    -o IdentitiesOnly=yes \
    -o IdentityAgent=none \
    -o BatchMode=yes \
    -o PreferredAuthentications=publickey \
    -o PasswordAuthentication=no \
    -o KbdInteractiveAuthentication=no \
    -o NumberOfPasswordPrompts=0 \
    -- "$host"
