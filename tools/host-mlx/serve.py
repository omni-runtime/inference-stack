#!/usr/bin/env python3
"""Explicit MLX host process. Accept a stack file or generated stdlib-only settings."""
import argparse
import json
import os
from pathlib import Path
import sys


def settings_from_args(args):
    if args.config:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
        from stack_config import StackConfig
        if not args.backend:
            raise ValueError('--config requires --backend')
        return StackConfig(args.config).mlx_settings(args.backend)
    if args.settings:
        return json.loads(args.settings.read_text())
    # Compatibility for existing launchd installations; lock metadata stays authoritative.
    root = Path(os.environ.get('MLX_HOME', '/opt/omni-runtime/host-mlx'))
    lock = json.loads(Path(__file__).with_name('versions.json').read_text())
    return dict(binary=str(root / 'bin/mlx-serve-macos-arm64/mlx-serve'),
                model=str(root / 'models/MiniMax-H3-FL2VA-4bit'), revision=lock['revision'],
                secret_file=str(root / 'secrets/MODEL_API_KEY'),
                listen=os.environ.get('MLX_HOST', '127.0.0.1'), port=11234, max_concurrent=1, timeout=3600)


def launch(settings):
    model = Path(settings['model'])
    verification = json.loads((model / 'verification.json').read_text())
    if verification['revision'] != settings['revision']:
        raise ValueError('model revision has not been verified')
    for item in verification['files']:
        target = (model / item['file']).resolve()
        if not target.is_relative_to(model.resolve()) or target.stat().st_size != item['bytes']:
            raise ValueError('verified model file path or size changed')
    key = Path(settings['secret_file']).read_text().strip()
    if not key:
        raise ValueError('empty model credential')
    environment = dict(os.environ, MLX_STACK_API_KEY=key)
    binary = settings['binary']
    command = [binary, '--model', str(model), '--serve', '--host', settings['listen'],
               '--port', str(settings['port']), '--api-key-env', 'MLX_STACK_API_KEY', '--api-key-strict',
               '--max-concurrent', str(settings['max_concurrent']), '--timeout', str(settings['timeout'])]
    os.execve(binary, command, environment)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--config', type=Path)
    source.add_argument('--settings', type=Path)
    parser.add_argument('--backend')
    args = parser.parse_args()
    try:
        launch(settings_from_args(args))
    except (ValueError, FileNotFoundError) as error:
        parser.exit(2, f'mlx-serve: {error}\n')


if __name__ == '__main__':
    main()
