#!/usr/bin/env bash
set -euo pipefail
[[ -f /.dockerenv && ( $PWD == /workspace/inference-stack || $PWD == /workspace/inference-stack/* ) ]] || {
  echo 'Project installer must run in the tools container at /workspace/inference-stack' >&2; exit 2;
}
root=$(cd "$(dirname "$0")/.." && pwd)
command -v docker >/dev/null || { echo 'use the locked tools image containing the Docker client' >&2; exit 2; }
python3 -m venv "$root/.venv"
"$root/.venv/bin/python" -m pip install -r "$root/requirements.txt"
exec "$root/.venv/bin/python" "$root/scripts/deploy.py" check "$@"
