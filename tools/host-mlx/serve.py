#!/usr/bin/env python3
"""Operator-owned host process; SR never starts or stops the model engine."""
import json
import os
from pathlib import Path

root = Path(os.environ.get('MLX_HOME', '/opt/omni-runtime/host-mlx'))
model = root / 'models/MiniMax-H3-FL2VA-4bit'
verification = json.loads((model / 'verification.json').read_text())
if verification['revision'] != '9c9273578ed5c2daf0b9a42eace823a6a49050ba':
    raise SystemExit('model revision has not been verified')
for item in verification['files']:
    if (model / item['file']).stat().st_size != item['bytes']:
        raise SystemExit('verified model file size changed')
key = (root / 'secrets/MODEL_API_KEY').read_text().strip()
if not key:
    raise SystemExit('empty model credential')
environment = dict(os.environ, MLX_STACK_API_KEY=key)
binary = root / 'bin/mlx-serve-macos-arm64/mlx-serve'
args = [str(binary), '--model', str(model), '--serve', '--host', os.environ.get('MLX_HOST', '127.0.0.1'),
        '--port', '11234', '--api-key-env', 'MLX_STACK_API_KEY', '--api-key-strict',
        '--max-concurrent', '1', '--timeout', '3600']
os.execve(binary, args, environment)
