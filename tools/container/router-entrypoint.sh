#!/bin/sh
set -eu
# Runtime credential injection only; this process does not handle requests.
for name in MODEL_API_KEY CLOUD_API_KEY; do
  if [ -f "/run/secrets/$name" ]; then
    value=$(cat "/run/secrets/$name")
    export "$name=$value"
  fi
done
exec /app/router-candle "$@"
