#!/usr/bin/env python3
"""Exercise native task bindings through real Docker SR/Envoy and Redis."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time

import yaml

from run import Run, ROOT


class DockerVideoLifecycle(Run):
    def execute(self):
        if self.args.runtime != 'docker' or self.args.overlay != 'mock':
            raise ValueError('this suite requires Docker and explicit mock backends')
        self.wait_gateway()
        d = self.environment['docker']
        docker = ['docker', '--context', d['context']]
        operations = []

        def operation(command):
            value = subprocess.check_output(command, text=True, stderr=subprocess.STDOUT, timeout=90)
            operations.append({'time': time.time(), 'command': command, 'output': value})
            (self.output / 'operations.json').write_text(json.dumps(operations, indent=2)+'\n')
            return value

        def compose(*parts):
            return operation(self.command + list(parts))

        def store_ready():
            compose('up', '-d', '--wait', '--wait-timeout', '60', '--no-deps', 'media-bindings')

        def store_down():
            compose('stop', '--timeout', '2', 'media-bindings')
            ids = compose('ps', '--all', '--quiet', 'media-bindings').split()
            assert ids and all(operation(docker+['inspect', '--format', '{{.State.Running}}', cid]).strip() == 'false' for cid in ids)

        volumes_before = operation(docker+['volume', 'inspect', '--format', '{{.Name}} {{.CreatedAt}}', d['volumes']['media_bindings']])
        base = yaml.safe_load((ROOT/'generated'/self.args.environment/self.args.example/'compose.common.yaml').read_text())
        original_mount = next(m for m in base['services']['router']['volumes'] if m['target'] == '/config')
        original_directory = Path(d['config_volume_mount']) / original_mount['volume']['subpath']
        config = (original_directory/'router.yaml').read_text()

        def router_generation(config_text=None, replicas=2):
            command = self.command.copy()
            if config_text is not None:
                generation = hashlib.sha256((str(original_directory)+'\n'+config_text).encode()).hexdigest()
                directory = Path(d['config_volume_mount'])/'releases'/generation
                shutil.copytree(original_directory, directory, dirs_exist_ok=True)
                (directory/'router.yaml').write_text(config_text)
                mount = {**original_mount, 'volume': {'subpath': 'releases/'+generation}}
                override = self.output/'router-override.yaml'
                override.write_text(yaml.safe_dump({'services': {'router': {
                    'volumes': [mount], 'labels': {'inference-stack.config-sha256': generation}}}}, sort_keys=False))
                command += ['--file', str(override)]
            operation(command+['up', '-d', '--wait', '--wait-timeout', '60', '--no-deps',
                              '--force-recreate', '--scale', f'router={replicas}', 'router'])
            self.wait_gateway()

        models = [m for m in self.models if m['api_format']=='video' and m['pool']=='omni']
        body = self.multipart_call('video-lifecycle-create', '/v1/videos',
            [('model','local-only'), ('prompt','A blue square')], [], [200],
            [m['service'] for m in models], kind='video-job')
        if self.records[-1]['error']:
            return self.collect()
        task_id = json.loads(body)['id']
        self.records[-1]['public_video_id'] = task_id
        backend = self.records[-1]['response_headers']['x-mock-backend']

        def query(name, statuses=(200,), reaches_backend=True):
            data = self.call(name, '/v1/videos/'+task_id, {}, list(statuses), [backend] if reaches_backend else [],
                method='GET', kind='video-job' if statuses==(200,) else 'service-unavailable',
                headers={'X-Selected-Model':'cloud/video-async', 'X-VSR-Skip-Processing':'true'})
            self.records[-1]['public_video_id'] = task_id
            if statuses==(200,) and not self.records[-1]['error'] and json.loads(data)['id']!=task_id:
                self.records[-1]['error'] = 'public task identity changed after restart'

        try:
            router_generation()
            query('video-restored-by-new-router-generation')
            store_down()
            query('video-binding-store-down', (503,), False)
            self.multipart_call('video-create-store-down', '/v1/videos',
                [('model','local-only'), ('prompt','Must not execute')], [], [503], [], kind='service-unavailable')
            store_ready()
            query('video-restored-after-redis-aof-restart')
            with ThreadPoolExecutor(max_workers=1) as executor:
                started = time.time()
                pending = executor.submit(self.multipart_call, 'video-created-binding-commit-failure', '/v1/videos',
                    [('model','local-only'), ('prompt','fixture:slow-task-response')], [], [503], [backend])
                deadline = time.monotonic()+10
                while True:
                    logs = self.run_command('logs', '--no-color', '--no-log-prefix', '--tail=100', backend)
                    events = [json.loads(line) for line in logs.splitlines() if line.startswith('{')]
                    if any(e.get('event')=='video_created' and e['time']>=started for e in events):
                        break
                    if time.monotonic()>=deadline:
                        raise RuntimeError('delayed task did not reach its backend')
                    time.sleep(0.1)
                store_down()
                if b'created but its binding could not be saved' not in pending.result(timeout=40):
                    self.records[-1]['error'] = 'creation uncertainty was not reported explicitly'
            store_ready()
            query('video-existing-binding-survives-unrelated-commit-failure')
            document = yaml.safe_load(config)
            instances = document['global']['stores']['media_bindings']['instances']
            for model in models:
                instances[model['name']] += '-changed'
            router_generation(yaml.safe_dump(document, sort_keys=False))
            query('video-rejects-changed-instance', (409,), False)
            router_generation()
            query('video-restored-after-explicit-config-rollback')
            script = "for _, k in ipairs(redis.call('KEYS','vsr:media-binding:v1:entry:*')) do local v=cjson.decode(redis.call('GET',k)); if v.key==ARGV[1] then return redis.call('PEXPIRE',k,1) end end; return 0"
            assert compose('exec', '-T', 'media-bindings', 'redis-cli', 'EVAL', script, '0', task_id).strip() == '1'
            query('video-expired-binding', (404,), False)
            assert operation(docker+['volume','inspect','--format','{{.Name}} {{.CreatedAt}}',d['volumes']['media_bindings']]) == volumes_before
        finally:
            store_ready()
            router_generation(replicas=1)
        return self.collect()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment', default='docker')
    parser.add_argument('--runtime', default='docker')
    parser.add_argument('--example', default='vllm-omni-cloud')
    parser.add_argument('--overlay', default='mock')
    parser.add_argument('--catalog', default='tests/overlays/mock/catalog-media.yaml')
    parser.add_argument('--url')
    raise SystemExit(bool(DockerVideoLifecycle(parser.parse_args()).execute()))
