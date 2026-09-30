#!/usr/bin/env python3
"""Read existing docker weights inside an explicitly mounted audit container.

No tensor loading, conversion, downloads, writes to the model mount, or engine
substitution. The report records format evidence; it does not assert inference.
"""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import struct
import sys

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from boundary import enforce_execution


def main():
    env = yaml.safe_load((ROOT / 'environments/docker/environment.yaml').read_text())
    enforce_execution(env)
    model = Path(env['docker']['model_target'])
    if not model.is_dir() or model.is_symlink():
        raise ValueError('the exact existing model directory is not mounted')
    mount_readonly = bool(os.statvfs(model).f_flag & os.ST_RDONLY)
    if not mount_readonly:
        raise ValueError('model filesystem is not read-only; no write probe is attempted')
    report = {'environment': 'docker', 'runtime': 'docker', 'test_type': 'readonly-model-audit',
              'container_architecture': platform.machine(), 'model_mount': str(model),
              'mount_readonly': mount_readonly, 'inference': 'not-executed',
              'engine_compatibility': 'requires separate engine/source verification',
              'metadata': {}, 'weight_files': [], 'skipped_links': [], 'errors': []}
    files = []
    for directory, dirs, names in os.walk(model, followlinks=False):
        for name in list(dirs):
            path = Path(directory) / name
            if path.is_symlink():
                report['skipped_links'].append(str(path.relative_to(model))); dirs.remove(name)
        for name in names:
            path = Path(directory) / name
            if path.is_symlink():
                report['skipped_links'].append(str(path.relative_to(model)))
            elif path.is_file(): files.append(path)
    report['file_count'] = len(files)
    report['total_file_bytes'] = sum(p.stat().st_size for p in files)
    report['extensions'] = dict(Counter(p.suffix for p in files))
    report['custom_code_files'] = [{'file': str(p.relative_to(model)), 'bytes': p.stat().st_size,
                                    'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                                   for p in files if p.suffix == '.py' and p.stat().st_size <= 2 * 1024 * 1024]
    report['license_documents'] = [{'file': str(p.relative_to(model)), 'bytes': p.stat().st_size,
                                    'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                                   for p in files if p.name.lower().startswith(('license', 'notice')) and p.stat().st_size <= 2 * 1024 * 1024]
    approved = {'architectures', 'model_type', 'torch_dtype', 'dtype', 'vocab_size',
                'hidden_size', 'num_hidden_layers', 'num_local_experts', 'num_experts_per_tok',
                'max_position_embeddings', 'quantization_config', 'quantization', '_commit_hash',
                '_class_name', '_diffusers_version', 'auto_map'}
    metadata_names = {'config.json', 'tokenizer_config.json', 'preprocessor_config.json',
                      'processor_config.json', 'model_index.json', 'quantization_config.json'}
    for path in sorted(files):
        if path.name not in metadata_names: continue
        filename = str(path.relative_to(model))
        if path.stat().st_size > 2 * 1024 * 1024:
            report['errors'].append(filename + ': metadata exceeds audit size bound'); continue
        data = path.read_bytes()
        try: document = json.loads(data)
        except ValueError:
            report['errors'].append(filename + ': invalid JSON'); continue
        if not isinstance(document, dict):
            report['errors'].append(filename + ': expected metadata object'); continue
        values = {k: document[k] for k in approved if k in document}
        for key in ('quantization', 'quantization_config'):
            if isinstance(values.get(key), dict):
                values[key] = {k: v for k, v in values[key].items() if k in {'quant_method', 'bits', 'group_size', 'mode', 'format', 'backend', 'version'}}
        for key in ('tokenizer_class', 'processor_class', 'feature_extractor_type'):
            if key in document: values[key] = document[key]
        report['metadata'][filename] = {'sha256': hashlib.sha256(data).hexdigest(), 'fields': values}
    for path in sorted(files):
        if path.suffix not in {'.safetensors', '.gguf', '.bin', '.pt', '.pth'}: continue
        item = {'file': str(path.relative_to(model)), 'bytes': path.stat().st_size, 'format': path.suffix[1:]}
        report['weight_files'].append(item)
        if path.suffix != '.safetensors': continue
        with path.open('rb') as stream:
            prefix = stream.read(8)
            if len(prefix) != 8:
                report['errors'].append(item['file'] + ': incomplete header'); continue
            length = struct.unpack('<Q', prefix)[0]
            if length > 16 * 1024 * 1024 or length + 8 > item['bytes']:
                report['errors'].append(item['file'] + ': invalid/bounded header length'); continue
            try: header = json.loads(stream.read(length))
            except ValueError:
                report['errors'].append(item['file'] + ': invalid tensor header'); continue
        if not isinstance(header, dict) or any(not isinstance(v, dict) for k,v in header.items() if k != '__metadata__'):
            report['errors'].append(item['file'] + ': expected tensor objects'); continue
        item['header_sha256'] = hashlib.sha256(json.dumps(header, sort_keys=True).encode()).hexdigest()
        metadata = header.get('__metadata__', {})
        if isinstance(metadata, dict):
            item['declared_format'] = metadata.get('format')
        tensors = {k: v for k, v in header.items() if k != '__metadata__'}
        item['tensor_count'] = len(tensors)
        item['stored_dtypes'] = dict(Counter(v.get('dtype', 'unknown') for v in tensors.values()))
        item['quantization_key_suffixes'] = dict(Counter(k.rsplit('.', 1)[-1] for k in tensors if k.endswith(('.scales', '.biases', '.qweight', '.qzeros', '.weight_scale'))))
        bad = [k for k, v in tensors.items() if not isinstance(v.get('data_offsets'), list) or len(v['data_offsets']) != 2
               or not 0 <= v['data_offsets'][0] <= v['data_offsets'][1] <= item['bytes'] - length - 8]
        if bad: report['errors'].append(item['file'] + ': invalid tensor offsets')
    report['container_visible_nvidia_devices'] = sorted(str(p) for p in Path('/dev').glob('nvidia*'))
    report['container_visible_dri'] = Path('/dev/dri').exists()
    report['container_limits'] = {p.name: p.read_text().strip() for p in
                                  (Path('/sys/fs/cgroup/memory.max'), Path('/sys/fs/cgroup/cpu.max')) if p.is_file()}
    report['status'] = 'failed' if report['errors'] else 'metadata-read'
    output = ROOT / 'reports/docker/docker' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-model-audit')
    output.mkdir(parents=True)
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(f'{output}: {report["status"]}; inference not executed')
    return bool(report['errors'])


if __name__ == '__main__':
    raise SystemExit(main())
