#!/bin/sh
set -eu
for file in /run/secrets/*_API_KEY; do
  [ -f "$file" ] || continue
  key=${file##*/}
  case "$key" in *[!A-Z0-9_]*) exit 2;; esac
  value=$(cat "$file")
  export "$key=$value"
done
exec "$@"
