#!/bin/sh
# Stage explicitly supplied, already pinned .deb files for offline apt.
# Usage: sh stage-debs.sh /absolute/archive-directory FILE.deb...
set -eu
[ "$#" -ge 2 ] || { echo 'usage: stage-debs.sh /absolute/directory FILE.deb...' >&2; exit 2; }
destination=$1
shift
case "$destination" in /*) ;; *) echo 'archive directory must be absolute' >&2; exit 2;; esac
mkdir -p "$destination"
staging=$(mktemp -d "$destination/.stage-debs.XXXXXXXX")
trap 'rm -rf "$staging"' EXIT
trap 'exit 1' HUP INT TERM
for input do
  package=$(dpkg-deb -f "$input" Package)
  version=$(dpkg-deb -f "$input" Version)
  architecture=$(dpkg-deb -f "$input" Architecture)
  case "$package" in ''|*[!a-z0-9+.-]*) echo 'invalid Debian package name' >&2; exit 1;; esac
  case "$version" in ''|*[!a-zA-Z0-9.+:~_-]*) echo 'invalid Debian version' >&2; exit 1;; esac
  case "$architecture" in ''|*[!a-z0-9-]*) echo 'invalid Debian architecture' >&2; exit 1;; esac
  archive_version=$(printf '%s' "$version" | sed 's/:/%3a/g')
  name="${package}_${archive_version}_${architecture}.deb"
  for existing in "$destination/$name" "$staging/$name"; do
    if [ -e "$existing" ] && ! cmp -s "$input" "$existing"; then
      echo "conflicting Debian archive: $name" >&2
      exit 1
    fi
  done
  cp "$input" "$staging/$name"
done
# Validate the whole supplied set before publishing any archive.
for archive in "$staging"/*.deb; do
  mv "$archive" "$destination/"
done
mkdir -p "$destination/partial"
