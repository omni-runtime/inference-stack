#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
exec "$root/.venv/bin/python" "$root/scripts/deploy.py" check "$@"
