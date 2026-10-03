"""Mixed-architecture placement, worker join and private ALP deployment contracts."""
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import cluster
import deploy
from stack_config import StackConfig


class ClusterConfiguration(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        source = ROOT / 'examples/hybrid/stack.yaml'
        self.doc = yaml.safe_load(source.read_text())
        self.doc['name'] = 'unit-cluster'
        for k, v in self.doc['artifacts'].items():
            self.doc['artifacts'][k] = str((source.parent / v).resolve())
        for backend in self.doc['backends'].values():
            for k in ('engine_lock', 'model_manifest'):
                if k in backend.get('mlx', {}):
                    backend['mlx'][k] = str((source.parent / backend['mlx'][k]).resolve())
        gateway = self.doc['gateway']
        self.doc['hosts']['gpu'] = {'ssh': 'gpu.example', 'root': '/srv/inference-stack'}
        self.doc['cluster'] = {
            'distribution': 'k3s', 'version': 'v1.36.4+k3s1', 'server': 'https://control.example:6443',
            'control_plane': {'host': gateway['host'], 'node': gateway['kubernetes']['node'],
                              'address': '192.0.2.10', 'platform': gateway['platform'], 'flannel_interface': 'eth0'},
            'workers': {'gpu': {'host': 'gpu', 'node': 'gpu-node', 'address': '192.0.2.20',
                               'platform': 'linux/amd64', 'flannel_interface': 'zt0', 'gpu_count': 1,
                               'runtime_class': 'nvidia', 'model_root': '/srv/models'}}}
        self.doc['backends']['vllm'].update(host='gpu', base_url='http://vllm:8000/v1',
            deployment={'mode': 'managed', 'node': 'gpu', 'image': 'omni', 'command': ['vllm', 'serve', '/models/text']})
        self.doc['resources']['model_memory'] = '8Gi'
        self.path = self.directory / 'stack.yaml'

    def config(self):
        self.path.write_text(yaml.safe_dump(self.doc))
        return StackConfig(self.path)

    def render(self):
        self.config()
        # Render into an isolated root while retaining checked-in templates.
        root = self.directory / 'runtime'
        root.mkdir(exist_ok=True)
        for name in ('deploy', 'config', 'tools', 'tests'):
            if not (root / name).exists():
                (root / name).symlink_to(ROOT / name)
        args = SimpleNamespace(config=self.path, environment=None, runtime=None, example=None,
            catalog=None, release=None, overlay=None, action='render', candidate_test=False, service=None)
        with patch.object(deploy, 'ROOT', root):
            stack = deploy.Stack(args)
            stack.render(stage=False)
        return {p.name: list(yaml.safe_load_all(p.read_text())) for p in stack.output.glob('*.yaml')}

    def test_gateway_and_managed_worker_have_distinct_placement_and_storage(self):
        files = self.render()
        router = files['deployment-router.yaml'][0]['spec']['template']['spec']
        model = files['deployment-models.yaml'][0]['spec']['template']['spec']
        self.assertEqual(router['nodeSelector']['kubernetes.io/arch'], 'arm64')
        self.assertEqual(model['nodeSelector'], {'kubernetes.io/hostname': 'gpu-node', 'kubernetes.io/arch': 'amd64'})
        self.assertEqual(model['runtimeClassName'], 'nvidia')
        volumes = {v['name']: v for v in model['volumes']}
        self.assertEqual(volumes['models']['hostPath']['path'], '/srv/models')
        self.assertEqual(volumes['cache']['persistentVolumeClaim']['claimName'], 'model-cache-vllm')
        self.assertEqual(files['storage.yaml'][0]['items'][0]['metadata']['name'], 'model-cache-vllm')

    def test_invalid_worker_configuration_fails_before_render(self):
        original = copy.deepcopy(self.doc)
        changes = [lambda d: d['backends']['vllm']['deployment'].update(node='absent'),
                   lambda d: d['backends']['vllm'].update(host='gateway'),
                   lambda d: d['cluster']['workers']['gpu'].update(platform='linux/arm64'),
                   lambda d: d['cluster']['workers']['gpu'].update(gpu_count=0),
                   lambda d: d['cluster']['control_plane'].update(node='wrong'),
                   lambda d: d['backends']['vllm'].update(base_url='http://192.0.2.20:30810/v1'),
                   lambda d: d['cluster'].update(server='http://insecure:6443')]
        for change in changes:
            self.doc = copy.deepcopy(original)
            change(self.doc)
            with self.assertRaises(ValueError):
                self.config()

    def test_agent_join_uses_one_server_and_external_token_file(self):
        result = cluster.agent_config(self.config(), 'gpu', '/private/join-token')
        self.assertEqual(result['server'], self.doc['cluster']['server'])
        self.assertEqual(result['node-ip'], '192.0.2.20')
        self.assertEqual(result['flannel-iface'], 'zt0')
        self.assertEqual(result['token-file'], '/private/join-token')
        self.assertNotIn('token', result)
        self.assertFalse(any(x.startswith('node-role.kubernetes.io/') for x in result['node-label']))
        self.assertNotIn('CLOUD_API_KEY', json.dumps(result))

    def test_live_check_rejects_two_independent_control_planes(self):
        cfg = self.config()
        nodes = []
        for n in [self.doc['cluster']['control_plane'], self.doc['cluster']['workers']['gpu']]:
            nodes.append({'metadata': {'name': n['node'], 'labels': {
                'kubernetes.io/arch': n['platform'].split('/')[1], 'node-role.kubernetes.io/control-plane': 'true'}},
                'status': {'conditions': [{'type': 'Ready', 'status': 'True'}],
                           'addresses': [{'type': 'InternalIP', 'address': n['address']}],
                           'nodeInfo': {'kubeletVersion': self.doc['cluster']['version']},
                           'allocatable': {'nvidia.com/gpu': '1'}}})
        self.assertFalse(cluster.inspect_nodes(cfg, {'items': nodes})[1]['valid'])
        del nodes[1]['metadata']['labels']['node-role.kubernetes.io/control-plane']
        self.assertTrue(all(x['valid'] for x in cluster.inspect_nodes(cfg, {'items': nodes})))

    def test_alp_worker_configuration_stays_in_unified_input(self):
        cloud = next(m for m in self.doc['models'] if m['backend'] == 'cloud')
        cloud['capabilities'].append('tools')
        self.doc['alp'] = {'enabled': True, 'worker_directory': '/srv/private-alp', 'worker_mount': '/opt/alp',
                           'python': '/usr/bin/python3', 'catalog_file': '/opt/alp/catalogs.json',
                           'task_key_env': 'ALP_TASK_KEY', 'projection': 'typed', 'models': {cloud['name']: {'enable_thinking': False}}}
        self.assertIn('ALP_TASK_KEY', self.config().required_keys)
        files = self.render()
        integration = files['router.yaml'][0]['global']['integrations']['alp']
        self.assertEqual(integration['worker_env'], ['PYTHONPATH', 'ALP_TASK_KEY'])
        self.assertEqual(integration['timeout_milliseconds'], 10000)
        self.assertEqual(integration['command'][-2:], ['--projection', 'typed'])
        spec = files['deployment-router.yaml'][0]['spec']['template']['spec']
        self.assertIn({'name': 'alp-worker', 'hostPath': {'path': '/srv/private-alp', 'type': 'Directory'}}, spec['volumes'])
        self.assertTrue(spec['containers'][0]['volumeMounts'][-1]['readOnly'])
        self.doc['alp']['request_strict'] = True
        strict = self.render()['router.yaml'][0]['global']['integrations']['alp']
        self.assertEqual(strict['command'][-1], '--request-strict')
        self.doc['alp'].pop('request_strict')
        self.doc['alp'].pop('projection')
        legacy = self.render()['router.yaml'][0]['global']['integrations']['alp']
        self.assertNotIn('--projection', legacy['command'])

    def test_migration_reuses_claims_without_overwriting_volume_binding(self):
        self.doc['backends']['vllm']['deployment']['existing_claims'] = {'cache': 'retained-cache', 'outputs': 'retained-outputs'}
        files = self.render()
        spec = files['deployment-models.yaml'][0]['spec']['template']['spec']
        self.assertEqual(spec['volumes'][1]['persistentVolumeClaim']['claimName'], 'retained-cache')
        self.assertEqual(files['storage.yaml'][0]['items'], [])

    def test_alp_requires_an_image_that_declares_the_interface(self):
        stack = SimpleNamespace(
            args=SimpleNamespace(candidate_test=False), mock=False,
            release={'status': 'preview', 'config': {'require_entrypoint': True},
                     'images': [{'platform': 'linux/arm64', 'acceptance': 'tested-with-real-backend',
                                 'reference': 'example/router@sha256:' + 'a' * 64,
                                 'interfaces': ['chat'], 'request_capabilities': ['text']}]},
            config=SimpleNamespace(document={'alp': {'enabled': True}}),
            environment={'platform': 'linux/arm64'},
            models=[{'api_format': 'openai', 'capabilities': ['text']}], images={})
        with self.assertRaisesRegex(ValueError, 'alp_chat'):
            deploy.Stack.check_contract(stack)
        stack.release['images'][0]['interfaces'].append('alp_chat')
        deploy.Stack.check_contract(stack)
        self.assertIn('router', stack.images)


if __name__ == '__main__':
    unittest.main()
