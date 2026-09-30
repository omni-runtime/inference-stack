#!/usr/bin/env python3
"""Reject forbidden configuration using validation only; never execute it."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from boundary import BoundaryError, enforce_execution, validate_compose


def main():
    env = yaml.safe_load((ROOT / 'environments/docker/environment.yaml').read_text())
    # Follow the selected environment execution boundary even for dry-run checks.
    enforce_execution(env)
    d = env['docker']
    config = {'name': env['project'], 'volumes': {'config': {'name': d['volumes']['config']}},
              'services': {'model': {'volumes': [{'type': 'volume', 'source': 'config',
                                                'target': '/config', 'read_only': True}]}}}
    validate_compose(config, env)
    fixtures = {}
    for key, value in [('privileged', True), ('pid', 'host'), ('ipc', 'host'), ('uts', 'host'),
                       ('cgroup', 'host'), ('network_mode', 'host'), ('cap_add', ['SYS_ADMIN']),
                       ('devices', ['/dev/nvidia0:/dev/nvidia0']), ('volumes_from', ['unrelated']),
                       ('post_start', [{'command': 'true', 'privileged': True}]),
                       ('ports', ['8000:8000'])]:
        item = copy.deepcopy(config); item['services']['model'][key] = value
        fixtures[key] = item
    mounts = {
        'host-home': {'type': 'bind', 'source': '/home/operator', 'target': '/host', 'read_only': True, 'bind': {'create_host_path': False}},
        'writable-weights': {'type': 'bind', 'source': d['allowed_readonly_model_source'], 'target': d['model_target'], 'read_only': False, 'bind': {'create_host_path': False}},
        'host-path-creation': {'type': 'bind', 'source': d['allowed_readonly_model_source'], 'target': d['model_target'], 'read_only': True},
        'docker-socket': {'type': 'bind', 'source': '/var/run/docker.sock', 'target': '/var/run/docker.sock', 'read_only': True},
        'writable-config': {'type': 'volume', 'source': 'config', 'target': '/config'},
        'short-mount': '/:/host',
    }
    for name, mount in mounts.items():
        item = copy.deepcopy(config); item['services']['model']['volumes'] = [mount]; fixtures[name] = item
    for key in ('management', 'source_a', 'source_b', 'reports'):
        item = copy.deepcopy(config); item['volumes'][key] = {'name': d['volumes'][key]}; fixtures['business-'+key] = item
    item = copy.deepcopy(config); item['volumes']['config']['driver_opts'] = {'type': 'none', 'o': 'bind', 'device': '/'}
    fixtures['volume-driver-host-bind'] = item
    item = copy.deepcopy(config); item['name'] = 'unrelated'; fixtures['wrong-project'] = item
    records = []
    for name, fixture in fixtures.items():
        try:
            validate_compose(fixture, env)
        except BoundaryError as error:
            records.append({'case': name, 'status': 'passed', 'rejection': str(error)})
        else:
            records.append({'case': name, 'status': 'failed', 'error': 'forbidden configuration accepted'})
    report = {'runtime': 'docker', 'environment': 'docker', 'method': 'configuration-validation-only',
              'forbidden_commands_executed': 0, 'cases': records,
              'passed': sum(r['status'] == 'passed' for r in records),
              'failed': sum(r['status'] == 'failed' for r in records)}
    output = ROOT / 'reports/docker/docker' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-boundary')
    output.mkdir(parents=True); (output / 'report.json').write_text(json.dumps(report, indent=2)+'\n')
    print(f'{output}: {report["passed"]} passed, {report["failed"]} failed')
    return bool(report['failed'])


if __name__ == '__main__':
    raise SystemExit(main())
