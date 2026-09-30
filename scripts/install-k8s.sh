#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
command -v kubectl >/dev/null || { echo 'kubectl is missing; follow docs/installation.md' >&2; exit 2; }
uv_bin=$(command -v uv || true)
if [[ -z "$uv_bin" && -x "$HOME/.local/bin/uv" ]]; then uv_bin="$HOME/.local/bin/uv"; fi
if [[ -n "$uv_bin" ]]; then
  if [[ ! -x "$root/.venv/bin/python" ]]; then
    "$uv_bin" venv --python 3.12 "$root/.venv"
  fi
  "$uv_bin" pip install --python "$root/.venv/bin/python" -r "$root/requirements.txt"
else
  python3 -m venv "$root/.venv"
  "$root/.venv/bin/python" -m pip install -r "$root/requirements.txt"
fi
exec "$root/.venv/bin/python" "$root/scripts/deploy.py" check "$@"
