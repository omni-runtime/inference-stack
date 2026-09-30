#!/bin/sh
set -eu
export VLLM_API_KEY=$(cat /run/secrets/MODEL_API_KEY)
exec "$@"
