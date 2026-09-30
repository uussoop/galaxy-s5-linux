#!/bin/sh -e
# Copyright 2020 Oliver Smith
# SPDX-License-Identifier: GPL-3.0-or-later

TOPDIR="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$TOPDIR"

test_path() {
	for file in $(git ls-tree --name-only HEAD "$1"); do
		# Subdir: recurse
		if [ -d "$file" ]; then
			test_path "$1/$file/"
			continue
		fi
		case "$file" in
			LICENSE|*.yml|*.c|*.md)
				echo "$file: not a shell script"
				;;
			*)
				echo "$file: checking with shellcheck"
				shellcheck \
					-x "$TOPDIR/pmos_install_functions" \
					"$file"
		esac
	done
}

test_path .

echo "Success!"
