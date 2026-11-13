#!/bin/sh
# Restore a content-addressed APT index snapshot at an original update boundary.
set -eu
if [ "$#" -ne 2 ]; then
    echo "usage: restore-apt-indexes.sh ARCHIVE SHA256" >&2
    exit 2
fi
archive=$1
expected=$2
case "$expected" in *[!0-9a-f]*|'') echo 'invalid index SHA256' >&2; exit 2;; esac
[ "${#expected}" -eq 64 ] || exit 2
printf '%s  %s\n' "$expected" "$archive" | sha256sum -c - >/dev/null
# Inputs are produced by the canonical authoring helper, which permits only
# regular files and directories. Reject unexpected paths before touching state.
members=$(tar -tzf "$archive")
printf '%s\n' "$members" | while IFS= read -r member; do
    case "$member" in /*|..|../*|*/../*|*/..) echo 'unsafe APT index member' >&2; exit 2;; esac
done
rm -rf /var/lib/apt/lists
mkdir -p /var/lib/apt/lists
tar --numeric-owner -xzpf "$archive" -C /var/lib/apt/lists
