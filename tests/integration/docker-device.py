#!/usr/bin/env python3
"""Record a real GPU device request using the locked lightweight probe image."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'scripts'))
from boundary import enforce_execution


def main():
    env = yaml.safe_load((ROOT/'environments/docker/environment.yaml').read_text())
    enforce_execution(env)
    images = yaml.safe_load((ROOT/'versions.lock.yml').read_text())['images']
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    output = ROOT/'reports/docker/docker'/(stamp+'-device-request')
    output.mkdir(parents=True)
    name = env['project']+'-gpu-probe-'+stamp.lower()
    base = ['docker', '--context', env['docker']['context']]
    report = {'environment': 'docker', 'runtime': 'docker', 'status': 'running',
              'inference': 'not-executed', 'commands': []}

    def command(args):
        record = {'command': base+args, 'started_at': datetime.now(timezone.utc).isoformat()}
        try:
            result = subprocess.run(base+args, capture_output=True, text=True, timeout=45)
            record.update(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)
        except subprocess.TimeoutExpired:
            record.update(returncode=None, timed_out=True)
        record['finished_at'] = datetime.now(timezone.utc).isoformat()
        report['commands'].append(record)
        (output/'report.json').write_text(json.dumps(report, indent=2)+'\n')
        return record

    created = False
    try:
        record = command(['create', '--name', name, '--label', 'inference-stack.project='+env['project'],
            '--platform', env['platform'], '--network', 'none', '--memory', '128m', '--cpus', '0.5',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true', '--gpus', 'all', images['python'],
            'python', '-c', 'import pathlib,json; print(json.dumps({"nvidia_devices": sorted(str(p) for p in pathlib.Path("/dev").glob("nvidia*"))}))'])
        created = record['returncode'] == 0
        if not created:
            report['status'] = 'blocked-at-container-create'
            return
        record = command(['start', name])
        if record['returncode'] != 0:
            report['status'] = 'blocked-gpu-device-request'
        else:
            wait = command(['wait', name])
            command(['logs', name])
            report['status'] = ('device-probe-executed-engine-not-tested' if
                wait['returncode'] == 0 and wait['stdout'].strip() == '0' else 'device-probe-failed')
        command(['inspect', '--format', '{{json .State}}', name])
    finally:
        if created:
            command(['rm', '-f', name])
        (output/'report.json').write_text(json.dumps(report, indent=2)+'\n')
        print(f'{output}: {report["status"]}; inference not executed')


if __name__ == '__main__':
    main()
