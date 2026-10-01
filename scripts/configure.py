#!/usr/bin/env python3
"""Migrate legacy deployment inputs into one stack.yaml without changing services."""
import argparse
import copy
import hashlib
import os
from pathlib import Path
import re

import yaml

from stack_config import ROOT, StackConfig, read_document, validate_document


def migrate(environment, example, catalog, output, mode, name=None, source_root=ROOT):
    source_root = Path(source_root).resolve()
    env = yaml.safe_load((source_root / 'environments' / environment / 'environment.yaml').read_text())
    pools = yaml.safe_load((source_root / 'examples' / example / 'example.yaml').read_text())['pools']
    source = yaml.safe_load((source_root / catalog).read_text())
    if source.get('mock_only') and mode != 'mock':
        raise ValueError('mock catalog requires --mode mock')
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('output already exists; migration never overwrites a configuration')
    relative = lambda path: os.path.relpath(source_root / path, output.parent)
    runtime = env['runtime']
    target = copy.deepcopy(env[runtime])
    host = {'ssh': target.pop('ssh'), 'root': env['root']}
    target.pop('project', None)
    if runtime == 'kubernetes' and not Path(target['kubeconfig']).is_absolute():
        target['kubeconfig'] = str((source_root / target['kubeconfig']).resolve())
    gateway = {'runtime':runtime, 'platform':env['platform'], 'project':env['project'],
               'host':'gateway', runtime:target}
    for field in ('container_only','verification_not_before'):
        if field in env:
            gateway[field] = env[field]
    doc = {'schema_version':1, 'name':name or environment, 'mode':mode,
           'hosts':{'gateway':host}, 'gateway':gateway, 'resources':env['resources'],
           'artifacts':{'release':relative(env['release']), 'versions':relative('versions.lock.yml')},
           'secrets':{'file':'secrets.env'}, 'backends':{}, 'models':[], 'routing':{'enabled_models':[]}}
    for original in source['models']:
        if original['pool'] not in pools:
            continue
        model = copy.deepcopy(original)
        # Historical acceptance annotations are evidence, not desired settings.
        # Keep the untouched source catalog as their provenance.
        model.pop('validation', None)
        service = model.pop('service')
        backend = {key:model.pop(key) for key in ('pool','provider','base_url','api_key_env')}
        deployment = model.pop('deployment', {'mode':'external'})
        deployment.setdefault('mode','managed')
        if 'config_files' in deployment:
            deployment['config_files'] = {filename:yaml.safe_load((source_root/path).read_text())
                                          for filename,path in deployment['config_files'].items()}
        backend['deployment'] = deployment
        if service in doc['backends'] and backend != doc['backends'][service]:
            raise ValueError('models disagree about shared backend ' + service)
        doc['backends'][service] = backend
        model['backend'] = service
        doc['models'].append(model)
        doc['routing']['enabled_models'].append(model['name'])
    validate_document(doc)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Open exclusively so a concurrent migration cannot replace another operator's file.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd,'w') as file:
        yaml.safe_dump(doc, file, sort_keys=False, allow_unicode=True)
    try:
        config = StackConfig(output)
    except Exception:
        output.unlink()
        raise
    sample = output.with_name('secrets.env.example')
    if not sample.exists():
        sample.write_text(''.join(f'{key}=replace-with-your-value\n' for key in sorted(config.required_keys)))
    return output


def import_release(source, output):
    """Copy an immutable B contract after structural checks, without changing its claims."""
    source, output = Path(source), Path(output)
    contract = read_document(source)
    if not isinstance(contract, dict) or contract.get('contract_version') != 1:
        raise ValueError('unsupported release contract')
    if contract.get('status') not in {'candidate','preview','released'} or not contract.get('images'):
        raise ValueError('release requires a status and build artifacts')
    platforms = set()
    for artifact in contract['images']:
        digest = artifact.get('digest','')
        if not re.fullmatch(r'sha256:[0-9a-f]{64}',digest) or not artifact.get('reference','').endswith('@'+digest):
            raise ValueError('release image must use its immutable sha256 digest')
        platform = artifact.get('platform')
        if platform not in {'linux/amd64','linux/arm64'} or platform in platforms:
            raise ValueError('release has an unsupported or duplicate platform')
        platforms.add(platform)
    payload = source.read_bytes()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('xb') as file:
        file.write(payload)
    return hashlib.sha256(payload).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    migrate_parser = sub.add_parser('migrate', help='convert legacy inputs; credentials are not copied')
    migrate_parser.add_argument('--environment',required=True)
    migrate_parser.add_argument('--example',required=True)
    migrate_parser.add_argument('--catalog',default='config/models/catalog.yaml')
    migrate_parser.add_argument('--mode',choices=['real','mock'],required=True)
    migrate_parser.add_argument('--name')
    migrate_parser.add_argument('--source-root',type=Path,default=ROOT)
    migrate_parser.add_argument('--output',type=Path,required=True)
    release_parser = sub.add_parser('import-release',help='import project B contract without editing or upgrading its acceptance claims')
    release_parser.add_argument('--source',type=Path,required=True)
    release_parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    try:
        if args.action == 'migrate':
            print(migrate(args.environment,args.example,args.catalog,args.output,args.mode,args.name,args.source_root))
        else:
            print('Imported release; sha256:',import_release(args.source,args.output))
    except (ValueError, FileNotFoundError, FileExistsError) as error:
        parser.exit(2,f'configure: {error}\n')


if __name__ == '__main__':
    main()
