#!/bin/sh
set -eu
key_name=${MODEL_KEY_NAME:-MODEL_API_KEY}
case "$key_name" in ''|*[!A-Z0-9_]*) exit 2;; esac
export VLLM_API_KEY=$(cat "/run/secrets/$key_name")
exec "$@"
