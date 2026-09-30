#!/usr/bin/env python3
"""Explicit operator lifecycle tests in the project's Docker tools container."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from boundary import enforce_execution


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment', default='docker')
    parser.add_argument('--catalog', default='tests/overlays/mock/catalog-media.yaml')
    args = parser.parse_args()
    env = yaml.safe_load((ROOT / 'environments' / args.environment / 'environment.yaml').read_text())
    if env['runtime'] != 'docker': raise ValueError('this suite requires Docker')
    enforce_execution(env)
    d = env['docker']
    output = ROOT / 'reports' / args.environment / 'docker' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-lifecycle')
    output.mkdir(parents=True)
    log = (output / 'operations.log').open('w')
    report = {'environment': args.environment, 'runtime': 'docker', 'backend_type': 'mock', 'cases': []}
    cli = [sys.executable, str(ROOT / 'scripts/deploy.py')]
    target = ['--runtime', 'docker', '--environment', args.environment]
    state_file = ROOT / '.state' / args.environment / 'docker/inventory.json'
    if 'vllm' in json.loads(state_file.read_text())['stopped']:
        raise ValueError('suite will not override a pre-existing explicit model stop')

    def command(parts, check=True, timeout=1000):
        log.write('$ ' + ' '.join(map(str, parts)) + '\n'); log.flush()
        result = subprocess.run(parts, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        log.write(result.stdout + '\n'); log.flush()
        if check and result.returncode: raise subprocess.CalledProcessError(result.returncode, parts)
        return result

    def docker(*parts, check=True):
        return command(['docker', '--context', d['context'], *parts], check=check, timeout=60)

    def deploy(example=None):
        extra = ['--example', example, '--overlay', 'mock', '--catalog', args.catalog] if example else []
        command(cli + ['deploy'] + target + extra)

    def checkpoint(name, **details):
        report['cases'].append({'case': name, 'status': 'passed', **details})
        (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        print(name + ': passed', flush=True)

    def containers(project=True):
        filters = ['--filter', 'label=com.docker.compose.project='+d['project']] if project else []
        ids = docker('ps', '-a', *filters, '--format', '{{.ID}}').stdout.split()
        if not ids: return []
        # Keep full inspect data in memory only; never log its environment values.
        command_line = ['docker', '--context', d['context'], 'inspect', *ids]
        log.write('$ ' + ' '.join(command_line) + ' [output sanitized in report]\n'); log.flush()
        items = json.loads(subprocess.check_output(command_line, text=True, timeout=60))
        if project:
            return items
        # Container ls does not support the negative label filter used by prune.
        # Select unrelated containers from the in-memory inspect response.
        return [c for c in items if all((c['Config'].get('Labels') or {}).get(label) != d['project']
                for label in ('com.docker.compose.project', 'inference-stack.project'))]

    def identity(items):
        return {c['Id']: {'name': c['Name'], 'started_at': c['State']['StartedAt'],
                         'restart_count': c['RestartCount'], 'status': c['State']['Status']}
                for c in items if c['Name'].lstrip('/') != d['tools_container']}

    def service_items(service):
        return [c for c in containers() if c['Config'].get('Labels', {}).get('com.docker.compose.service') == service]

    def running(service):
        return [c for c in service_items(service) if c['State']['Running']]

    def volume_identity():
        text = docker('volume', 'inspect', '--format', '{{.Name}} {{.CreatedAt}}', *d['volumes'].values()).stdout
        return sorted(text.splitlines())

    restored = False
    port_probe = None
    unrelated = identity(containers(False))
    try:
        for _ in range(2):
            command(['bash', 'scripts/install.sh', *target, '--example', 'vllm-omni-cloud', '--overlay', 'mock', '--catalog', args.catalog])
        checkpoint('repeated-install-reuses-dependencies')
        deploy('vllm-omni-cloud'); before = identity(containers()); deploy('vllm-omni-cloud')
        assert identity(containers()) == before
        checkpoint('repeated-deploy-keeps-container-identities', containers=before)
        deploy('local-vllm')
        command(cli + ['stop', *target, '--service', 'vllm']); assert not running('vllm')
        deploy(); assert not running('vllm')
        checkpoint('explicit-stop-survives-redeploy')
        before = identity(containers()); token = (ROOT / 'secrets' / args.environment / 'GATEWAY_TOKEN').read_text().strip()
        rid = str(uuid.uuid4())
        request = urllib.request.Request('http://envoy:8000/v1/chat/completions',
                  json.dumps({'model': 'local-only', 'messages': [{'role': 'user', 'content': 'Operator stop test'}], 'max_tokens': 32}).encode(),
                  {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json', 'X-Request-ID': rid})
        try: response = urllib.request.urlopen(request, timeout=30)
        except urllib.error.HTTPError as error: response = error
        with response: body = response.read(1048576); status = response.status
        assert status in (502, 503, 504), status
        assert identity(containers()) == before and not running('vllm')
        evidence = []
        deadline = time.monotonic() + 10
        while not evidence and time.monotonic() < deadline:
            lines = docker('logs', '--tail', '200', running('envoy')[0]['Id']).stdout.splitlines()
            for line in lines:
                try: event = json.loads(line)
                except ValueError: continue
                if isinstance(event, dict) and event.get('request_id') == rid: evidence.append(event)
            if not evidence: time.sleep(0.1)
        eligible = {m['name'] for m in yaml.safe_load((ROOT / args.catalog).read_text())['models']
                    if m['pool'] == 'vllm' and m['service'] == 'vllm' and m['api_format'] == 'openai'}
        assert len(evidence) == 1 and evidence[0].get('selected_model') in eligible
        checkpoint('request-does-not-start-stopped-model', request_id=rid, status_code=status,
                   response_sha256=hashlib.sha256(body).hexdigest(), native_envoy_evidence=evidence)
        command(cli + ['start', *target, '--service', 'vllm']); assert len(running('vllm')) == 1
        checkpoint('explicit-start-restores-model')
        # Fault injection replaces only this project's Envoy with a failed
        # container. Its configuration hash forces native Compose recreation.
        old = running('envoy'); assert len(old) == 1
        old_name = old[0]['Name'].lstrip('/')
        docker('rm', '-f', old[0]['Id'])
        images = yaml.safe_load((ROOT / 'versions.lock.yml').read_text())['images']
        failed = docker('create', '--name', old_name, '--platform', env['platform'],
                        '--label', 'com.docker.compose.project=' + d['project'],
                        '--label', 'com.docker.compose.service=envoy',
                        '--label', 'com.docker.compose.config-hash=intentional-lifecycle-fault',
                        '--label', 'com.docker.compose.container-number=1',
                        '--memory', '128m', '--cap-drop', 'ALL', images['python'],
                        'python', '-c', 'raise SystemExit(13)').stdout.strip()
        docker('start', '-a', failed, check=False)
        failed_item = service_items('envoy'); assert len(failed_item) == 1
        assert failed_item[0]['State']['ExitCode'] == 13
        deploy(); assert len(running('envoy')) == 1
        assert running('envoy')[0]['Id'] != failed
        checkpoint('explicit-deploy-recovers-failed-service', injected_container=failed)
        volumes = volume_identity()
        private = {p.name: p.read_bytes() for p in (ROOT / 'secrets' / args.environment).iterdir() if p.is_file()}
        marker = Path('/outputs') / ('lifecycle-' + uuid.uuid4().hex); marker.write_text('retained project output\n')
        command(cli + ['down', *target]); assert containers() == []
        assert volume_identity() == volumes and marker.read_text() == 'retained project output\n'
        assert {p.name: p.read_bytes() for p in (ROOT / 'secrets' / args.environment).iterdir() if p.is_file()} == private
        checkpoint('down-retains-volumes-credentials-and-output', volumes=volumes, credentials_preserved=True)
        port_probe = docker('create', '--name', d['project'] + '-port-probe-' + uuid.uuid4().hex[:8],
                            '--label', 'inference-stack.project=' + d['project'],
                            '--platform', env['platform'], '--memory', '128m', '--cpus', '0.5',
                            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                            '--publish', f"{d['gateway_bind']}:{d['gateway_port']}:8000",
                            images['python'], 'python', '-c', 'import time; time.sleep(600)').stdout.strip()
        docker('start', port_probe)
        before = identity(containers())
        result = command(cli + ['check', *target, '--example', 'local-vllm', '--overlay', 'mock', '--catalog', args.catalog], check=False)
        assert result.returncode and 'already published by another container' in result.stdout
        assert identity(containers()) == before
        checkpoint('gateway-port-conflict-rejected-before-mutation', exit_code=result.returncode, owned_port_probe=port_probe)
        docker('rm', '-f', port_probe); port_probe = None
        deploy(); assert volume_identity() == volumes and marker.is_file()
        checkpoint('deploy-after-down-reuses-persistent-data')
        deploy('cloud-only')
        models = [m for m in yaml.safe_load((ROOT / args.catalog).read_text())['models'] if m['pool'] == 'cloud']
        expected = {'router', 'envoy'} | {m['service'] for m in models}
        if any(m['api_format'] == 'video' for m in models): expected.add('media-bindings')
        actual = {c['Config']['Labels']['com.docker.compose.service'] for c in containers()}
        assert actual == expected, (actual, expected)
        checkpoint('example-switch-removes-owned-orphans', services=sorted(actual))
        release = yaml.safe_load((ROOT / env['release']).read_text())
        release['images'] = [dict(i, platform='linux/amd64') for i in release['images']]
        wrong = output / 'wrong-platform-release.yaml'; wrong.write_text(yaml.safe_dump(release))
        before = identity(containers())
        result = command(cli + ['check', *target, '--example', 'cloud-only', '--release', str(wrong)], check=False)
        assert result.returncode and 'no verified SR image' in result.stdout and identity(containers()) == before
        checkpoint('wrong-image-platform-rejected-before-mutation', exit_code=result.returncode)
        result = command(cli + ['check', *target, '--example', 'local-vllm'], check=False)
        assert result.returncode and 'compatible GPU' in result.stdout and identity(containers()) == before
        checkpoint('missing-gpu-rejected-before-mutation', exit_code=result.returncode)
        assert identity(containers(False)) == unrelated
        checkpoint('unrelated-container-start-and-restart-state-retained', unrelated_containers=unrelated)
        deploy('vllm-omni-cloud'); restored = True
        checkpoint('restore-full-explicit-mock-example')
    except Exception as error:
        report['cases'].append({'case': 'lifecycle-error', 'status': 'failed', 'error': str(error)})
    finally:
        if port_probe:
            docker('rm', '-f', port_probe, check=False)
        if not restored:
            try:
                if 'vllm' in json.loads(state_file.read_text())['stopped']:
                    command(cli + ['start', *target, '--service', 'vllm'])
                deploy('vllm-omni-cloud')
                report['recovery'] = 'restored explicit mock deployment'
            except Exception as error: report['recovery'] = str(error)
        report['passed'] = sum(c['status'] == 'passed' for c in report['cases'])
        report['failed'] = sum(c['status'] != 'passed' for c in report['cases'])
        (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n'); log.close()
    print(f'{output}: {report["passed"]} passed, {report["failed"]} failed', flush=True)
    return bool(report['failed'])


if __name__ == '__main__':
    raise SystemExit(main())
