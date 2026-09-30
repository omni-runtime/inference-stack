#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
runtime=
for ((i=1; i<=$#; i++)); do
  if [[ ${!i} == --runtime ]]; then j=$((i+1)); runtime=${!j}; fi
done
case "$runtime" in
  kubernetes) exec bash "$root/scripts/install-k8s.sh" "$@" ;;
  docker) exec bash "$root/scripts/install-docker.sh" "$@" ;;
  *) echo '--runtime kubernetes|docker is required' >&2; exit 2 ;;
esac
