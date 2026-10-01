#!/bin/sh
set -eu
# Runtime credential injection only; this process does not handle requests.
for file in /run/secrets/*_API_KEY; do
  [ -f "$file" ] || continue
  name=${file##*/}
  case "$name" in *[!A-Z0-9_]*) exit 2;; esac
  value=$(cat "$file")
  export "$name=$value"
done
exec /app/router-candle "$@"
