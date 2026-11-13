#!/bin/sh
# Replay an exact APK transaction without making dependency archives world roots.
set -eu
archives=$1
shift
test "$#" -gt 0
for package in "$@"; do
    case "$package" in
        ''|-*|*[!a-zA-Z0-9_+.@=:~-]*) echo "Invalid APK package root" >&2; exit 2 ;;
    esac
done
test -d "$archives"
test -f /etc/apk/world
world=$(mktemp)
cp -p /etc/apk/world "$world"
cleanup() { rm -f "$world"; }
trap cleanup EXIT HUP INT TERM
# An empty closure is valid when every requested package is already installed.
found=false
for archive in "$archives"/*.apk; do
    if test -f "$archive"; then found=true; break; fi
done
if "$found"; then
    apk add --no-cache --no-network "$archives"/*.apk
    cp -p "$world" /etc/apk/world
fi
apk add --no-cache --no-network "$@"
