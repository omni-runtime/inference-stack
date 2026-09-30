#!/usr/bin/env bash
set -euo pipefail
host=${1:-127.0.0.1}
port=${2:-8000}
path=${3:-/health}
exec 3<>"/dev/tcp/$host/$port"
printf 'GET %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n' "$path" "$host" >&3
IFS= read -r status <&3
[[ "$status" == *" 200 "* ]]
