#!/bin/sh
set -eu
for key in MODEL_API_KEY CLOUD_API_KEY; do
  if [ -f "/run/secrets/$key" ]; then
    value=$(cat "/run/secrets/$key")
    export "$key=$value"
  fi
done
exec "$@"
