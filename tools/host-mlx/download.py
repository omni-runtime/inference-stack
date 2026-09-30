#!/usr/bin/env python3
"""Resume the pinned H3 pack and verify every file before marking it usable."""
import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path
import subprocess
import time
from urllib.parse import quote


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=Path('/opt/omni-runtime/host-mlx/models/MiniMax-H3-FL2VA-4bit'))
    parser.add_argument('--endpoint', choices=['https://huggingface.co', 'https://hf-mirror.com'], default='https://huggingface.co')
    args = parser.parse_args()
    manifest = json.loads(Path(__file__).with_name('model-manifest.json').read_text())
    root = args.directory.resolve()
    root.mkdir(parents=True, exist_ok=True)

    def download(item):
        name = item['rfilename']
        destination = (root / name).resolve()
        if not destination.is_relative_to(root):
            raise ValueError('model file escapes destination')
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + '.partial')
        url = args.endpoint + '/' + manifest['id'] + '/resolve/' + manifest['sha'] + '/' + quote(name)
        if not destination.exists():
            for attempt in range(20):
                print('Downloading', name, 'attempt', attempt + 1, flush=True)
                result = subprocess.run(['curl', '-fsSL', '--connect-timeout', '20', '--speed-limit', '1024',
                                         '--speed-time', '30', '--max-time', '1800', '-C', '-', '-o', str(partial), url])
                if result.returncode == 0:
                    break
                time.sleep(5)
            if result.returncode:
                raise RuntimeError('download failed: ' + name)
            partial.replace(destination)
        if destination.stat().st_size != item['size']:
            raise ValueError('size mismatch: ' + name)
        algorithm = 'sha256' if item.get('lfs') else 'sha1'
        digest = hashlib.new(algorithm)
        if algorithm == 'sha1':
            digest.update(('blob ' + str(item['size']) + '\0').encode())
        with destination.open('rb') as source:
            for chunk in iter(lambda: source.read(8 * 1024 * 1024), b''):
                digest.update(chunk)
        expected = item['lfs']['sha256'] if item.get('lfs') else item['blobId']
        if digest.hexdigest() != expected:
            raise ValueError('hash mismatch: ' + name)
        print('Verified', name, flush=True)
        return {'file': name, 'bytes': item['size'], 'hash': digest.hexdigest(), 'algorithm': algorithm}

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        files = list(pool.map(download, manifest['siblings']))
    temporary = root / 'verification.json.tmp'
    temporary.write_text(json.dumps({'revision': manifest['sha'], 'files': files}, indent=2) + '\n')
    temporary.replace(root / 'verification.json')
    print('All', len(files), 'files verified.', flush=True)


if __name__ == '__main__':
    main()
