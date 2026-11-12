#!/bin/sh
# Install a pinned offline apt transaction and retain explicit package intent.
# Usage: sh install-debs.sh STAGE-DEBS-SCRIPT INPUT-DIRECTORY PACKAGE[=VERSION]...
# INPUT-DIRECTORY contains the complete selected .deb transaction, not a repo.
set -eu
allow_downgrades=
repair=
local_debs=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --allow-downgrades) allow_downgrades=--allow-downgrades; shift ;;
    --repair) repair=--fix-broken; shift ;;
    --local-deb)
      [ "$#" -ge 2 ] || { echo 'missing local deb path' >&2; exit 2; }
      case "$2" in /*.deb) ;; *) echo 'local deb must be an absolute .deb path' >&2; exit 2;; esac
      local_debs="$local_debs
$2"
      shift 2
      ;;
    --*) echo "unknown option: $1" >&2; exit 2 ;;
    *) break ;;
  esac
done
[ "$#" -ge 2 ] || { echo 'usage: install-debs.sh [OPTIONS] STAGER INPUT-DIRECTORY PACKAGE...' >&2; exit 2; }
[ "$#" -ge 3 ] || [ -n "$repair" ] || { echo 'explicit roots required outside repair mode' >&2; exit 2; }
stager=$1
inputs=$2
shift 2
work=$(mktemp -d /tmp/mango-apt.XXXXXXXX)
trap 'rm -rf "$work"' EXIT
trap 'exit 1' HUP INT TERM
apt-mark showmanual > "$work/manual-before"
LC_ALL=C sort -u "$work/manual-before" -o "$work/manual-before"
for specification do
  package=${specification%%=*}
  case "$package" in ''|*[!a-z0-9+.:_-]*) echo "invalid explicit apt package: $package" >&2; exit 2;; esac
  printf '%s\n' "$package"
done > "$work/requested"
# A resolved transaction may be empty when every requested root is installed.
# Missing roots still fail the checks below; an empty inventory is not success.
if (set -- "$inputs"/*.deb; [ -f "$1" ]); then
  sh "$stager" "$work/archives" "$inputs"/*.deb
fi
printf '%s\n' "$local_debs" | while IFS= read -r archive; do
  [ -z "$archive" ] || sh "$stager" "$work/archives" "$archive"
done
if (set -- "$work/archives"/*.deb; [ -f "$1" ]); then
  # apt needs a writable canonical archive cache even when all files are local.
  # Supplying only this transaction avoids stale files selecting earlier versions.
  apt-get -o "Dir::Cache::Archives=$work/archives" install -y --no-download --no-install-recommends $allow_downgrades $repair "$work/archives"/*.deb
elif [ -n "$repair" ]; then
  apt-get install -y --no-download --no-install-recommends $allow_downgrades --fix-broken
fi
# Versioned roots must match the authored transaction rather than whichever
# local archive happened to be supplied.
for specification do
  package=${specification%%=*}
  status=$(dpkg-query -W -f '${Status}' "$package")
  [ "$status" = 'install ok installed' ] || { echo "requested package is not installed: $package" >&2; exit 1; }
  case "$specification" in
    *=*)
      package=${specification%%=*}
      expected=${specification#*=}
      actual=$(dpkg-query -W -f '${Version}' "$package")
      [ "$actual" = "$expected" ] || { echo "version mismatch for $package: $actual != $expected" >&2; exit 1; }
      ;;
  esac
done
# Installing local dependency files marks them manual. Restore the prior manual
# roots plus the explicitly requested packages; newly selected dependencies stay
# automatic, including upgrades of previously automatic packages.
apt-mark showmanual > "$work/manual-after"
LC_ALL=C sort -u "$work/manual-after" -o "$work/manual-after"
cat "$work/manual-before" "$work/requested" | LC_ALL=C sort -u > "$work/manual-wanted"
LC_ALL=C comm -23 "$work/manual-after" "$work/manual-wanted" > "$work/automatic"
while IFS= read -r package; do
  [ -z "$package" ] || apt-mark auto "$package"
done < "$work/automatic"
while IFS= read -r package; do
  [ -z "$package" ] || apt-mark manual "$package"
done < "$work/requested"
