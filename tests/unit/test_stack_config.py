"""Unified configuration migration, isolation and offline rendering contracts."""
import copy
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import configure
import credentials
import deploy
from stack_config import StackConfig


def arguments(**overrides):
    values = dict(config=None, environment=None, runtime=None, example=None, catalog=None,
                  release=None, overlay=None, action='render', service=None,
                  candidate_test=False, config_only=True)
    values.update(overrides)
    return SimpleNamespace(**values)


class UnifiedConfiguration(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / 'runtime'
        self.root.mkdir()
        for name in ('config','environments'):
            shutil.copytree(ROOT / name, self.root / name)
        for name in ('deploy','tools','tests','examples','contracts','versions.lock.yml'):
            (self.root / name).symlink_to(ROOT / name)
        self.patch = patch.object(deploy, 'ROOT', self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.document = yaml.safe_load((ROOT / 'examples/hybrid/stack.yaml').read_text())
        self.document['name'] = 'unit-stack'
        self.document['artifacts'] = {k: str((ROOT / 'examples/hybrid' / v).resolve())
                                      for k,v in self.document['artifacts'].items()}
        mlx = self.document['backends']['mlx-h3']['mlx']
        for key in ('engine_lock','model_manifest'):
            mlx[key] = str((ROOT / 'examples/hybrid' / mlx[key]).resolve())
        self.path = self.directory / 'stack.yaml'

    def write(self):
        self.path.write_text(yaml.safe_dump(self.document, sort_keys=False))
        return self.path

    def stack(self, **kwargs):
        return deploy.Stack(arguments(config=self.write(), **kwargs))

    def rendered(self, stack):
        stack.render(stage=False)
        result = {p.name: list(yaml.safe_load_all(p.read_text())) for p in stack.output.glob('*.yaml')
                  if p.name != 'resolved.yaml'}
        # Legacy config files retain their original YAML bytes; inline unified
        # configuration serializes the same mapping again. Their content hashes
        # (and paths derived from those hashes) may differ without behavior drift.
        # Compare the parsed configuration and all other deployment fields.
        return json.loads(json.dumps(result).replace(stack.state['config_sha'], '<config-generation>'))

    def test_migration_preserves_all_seven_kubernetes_renderings(self):
        for example in sorted((ROOT / 'examples').glob('*/example.yaml')):
            with self.subTest(example=example.parent.name):
                name = example.parent.name
                target = self.directory / name / 'stack.yaml'
                configure.migrate('kubernetes',name,'config/models/catalog.yaml',target,'mock')
                legacy = deploy.Stack(arguments(environment='kubernetes',runtime='kubernetes',example=name,overlay='mock'))
                unified = deploy.Stack(arguments(config=target))
                self.assertEqual(self.rendered(legacy), self.rendered(unified))

    def test_hybrid_real_routing_and_envoy_are_preserved(self):
        legacy = deploy.Stack(arguments(environment='hybrid',runtime='kubernetes',example='vllm-omni-cloud',
                                        catalog='environments/hybrid/catalog.yaml'))
        new = self.stack()
        old_files = self.rendered(legacy)
        new_files = self.rendered(new)
        self.assertEqual(old_files, new_files)
        self.assertEqual(new.enabled_services, ['router','envoy'])
        settings = json.loads((new.output / 'host-mlx/mlx-h3/settings.json').read_text())
        self.assertEqual(settings['listen'],'127.0.0.1')
        self.assertEqual(settings['revision'],new.models[1]['revision'])

    def test_docker_migration_preserves_compose_and_routing(self):
        with patch.object(deploy,'enforce_execution'):
            legacy = deploy.Stack(arguments(environment='docker',runtime='docker',example='vllm-omni-cloud',overlay='mock'))
        target = self.directory / 'docker-migrated' / 'stack.yaml'
        configure.migrate('docker','vllm-omni-cloud','config/models/catalog.yaml',target,'mock')
        unified = deploy.Stack(arguments(config=target))
        self.assertEqual(self.rendered(legacy),self.rendered(unified))

    def test_state_never_overrides_selected_mode_models_or_release(self):
        state = self.root / '.state/unit-stack/kubernetes/inventory.json'
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({'overlay':'mock','example':'cloud-only','catalog':'missing.yaml',
                                     'stopped':['router'],'resources':[]}))
        stack = self.stack()
        self.assertFalse(stack.mock)
        self.assertEqual(len(stack.models),3)
        self.assertEqual(stack.state['stopped'],['router'])
        files = self.rendered(stack)
        self.assertEqual(files['deployment-router.yaml'][0]['spec']['replicas'],0)

    def test_legacy_command_explains_how_to_open_migrated_state(self):
        state = self.root / '.state/hybrid/kubernetes/inventory.json'
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({'config_path':str(self.path),'stopped':[],'resources':[]}))
        with self.assertRaisesRegex(ValueError,'pass --config'):
            deploy.Stack(arguments(environment='hybrid',runtime='kubernetes',action='status'))

    def test_unknown_fields_duplicate_keys_and_secret_errors_are_redacted(self):
        self.document['gateway']['unrecognized'] = 'SENTINEL-PRIVATE'
        with self.assertRaises(ValueError) as error:
            StackConfig(self.write())
        self.assertIn('gateway',str(error.exception))
        self.assertNotIn('SENTINEL-PRIVATE',str(error.exception))
        del self.document['gateway']['unrecognized']
        self.write()
        with self.path.open('a') as file:
            file.write('name: duplicate\n')
        with self.assertRaisesRegex(ValueError,'duplicate'):
            StackConfig(self.path)
        self.path.write_text('name: [SENTINEL-PRIVATE\n')
        with self.assertRaises(ValueError) as error:
            StackConfig(self.path)
        self.assertNotIn('SENTINEL-PRIVATE',str(error.exception))

    def test_unknown_backend_model_and_embedded_credentials_are_rejected(self):
        original = copy.deepcopy(self.document)
        changes = [lambda d:d['models'][0].update(backend='missing'),
                   lambda d:d['routing']['enabled_models'].append('missing'),
                   lambda d:d['backends']['vllm'].update(base_url='https://user:SENTINEL-PRIVATE@example.test/v1'),
                   lambda d:d['backends']['vllm']['deployment'].update(command=['bad'])]
        for change in changes:
            self.document = copy.deepcopy(original)
            change(self.document)
            with self.assertRaises(ValueError) as error:
                StackConfig(self.write())
            self.assertNotIn('SENTINEL-PRIVATE',str(error.exception))

    def test_mixed_cli_inputs_are_rejected(self):
        with self.assertRaisesRegex(ValueError,'cannot be combined'):
            self.stack(runtime='kubernetes')

    def test_duplicate_gateway_and_model_node_ports_are_rejected(self):
        backend = self.document['backends']['vllm']
        backend.pop('host')
        backend['deployment'] = {'mode':'managed','image':'vllm','command':['vllm','serve'],
                                 'node_port':self.document['gateway']['kubernetes']['gateway_node_port']}
        with self.assertRaisesRegex(ValueError,'duplicate port'):
            StackConfig(self.write())

    def test_disabled_model_is_not_rendered(self):
        self.document['routing']['enabled_models'] = ['cloud/chat']
        stack = self.stack()
        self.assertEqual(stack.pools,['cloud'])
        self.assertEqual([m['name'] for m in stack.models],['cloud/chat'])
        self.assertNotIn('vllm',stack.enabled_services)

    def test_secrets_are_read_literally_and_never_enter_render_or_plan(self):
        sentinel = 'SENTINEL-PRIVATE-$(must-not-run)'
        (self.directory / 'secrets.env').write_text('\n'.join(f'{k}={sentinel}' for k in ['GATEWAY_TOKEN','MODEL_API_KEY','CLOUD_API_KEY']))
        stack = self.stack()
        self.assertEqual(stack.config.credentials()['CLOUD_API_KEY'],sentinel)
        self.rendered(stack)
        output = io.StringIO()
        with redirect_stdout(output):
            stack.plan()
        self.assertNotIn(sentinel,output.getvalue())
        self.assertFalse(stack.state_file.exists())
        for path in stack.output.rglob('*'):
            if path.is_file():
                self.assertNotIn(sentinel,path.read_text())

    def test_missing_credentials_fail_without_revealing_other_values(self):
        (self.directory/'secrets.env').write_text('CLOUD_API_KEY=SENTINEL-PRIVATE\n')
        with self.assertRaises(ValueError) as error:
            StackConfig(self.write()).credentials()
        self.assertIn('GATEWAY_TOKEN',str(error.exception))
        self.assertNotIn('SENTINEL-PRIVATE',str(error.exception))

    def test_offline_docker_check_does_not_call_engine_or_stage_live_volume(self):
        config = ROOT / 'examples/docker/stack.yaml'
        with patch.object(deploy,'execute',side_effect=AssertionError('engine called')), \
             patch.object(deploy.Stack,'stage_compose_config',side_effect=AssertionError('live volume changed')), \
             patch.object(deploy,'enforce_execution',side_effect=AssertionError('host operation checked')):
            stack = deploy.Stack(arguments(config=config,action='check'))
            with redirect_stdout(io.StringIO()):
                stack.operate()
            self.assertTrue((stack.output/'compose.yaml').exists())
        # Actual operations still enforce the tools-container boundary.
        with patch.object(deploy,'enforce_execution',side_effect=ValueError('boundary retained')):
            with self.assertRaisesRegex(ValueError,'boundary retained'):
                deploy.Stack(arguments(config=config,action='deploy'))

    def test_plan_preserves_active_output_and_reports_changes_from_successful_baseline(self):
        stack = self.stack()
        self.rendered(stack)
        stack.state['applied_snapshot'] = stack.snapshot()
        stack.save()
        active = {p: p.read_bytes() for p in stack.output.rglob('*') if p.is_file()}
        self.document['backends']['vllm']['base_url'] = 'http://192.0.2.99:30810/v1'
        changed = self.stack(action='plan')
        output = io.StringIO()
        with patch.object(deploy,'execute',side_effect=AssertionError('runtime called')), redirect_stdout(output):
            changed.operate()
        plan = json.loads(output.getvalue())
        self.assertEqual(plan['update_may_restart'],['envoy','router'])
        self.assertEqual(active,{p:p.read_bytes() for p in active})

    def test_target_switch_requires_a_new_instance(self):
        stack = self.stack()
        stack.state['target'] = stack.config.target()
        stack.save()
        self.document['gateway']['kubernetes']['namespace'] = 'different'
        with self.assertRaisesRegex(ValueError,'target changed'):
            self.stack()

    def test_failed_generation_keeps_last_complete_output(self):
        stack = self.stack()
        self.rendered(stack)
        before = {p.relative_to(stack.output):p.read_bytes() for p in stack.output.rglob('*') if p.is_file()}
        with patch.object(deploy,'render_envoy',side_effect=ValueError('invalid config')):
            with self.assertRaisesRegex(ValueError,'invalid config'):
                stack.render(stage=False)
        after = {p.relative_to(stack.output):p.read_bytes() for p in stack.output.rglob('*') if p.is_file()}
        self.assertEqual(before,after)

    def test_snapshot_is_committed_only_after_successful_deploy(self):
        stack = self.stack(action='deploy')
        def successful_call(*args, **kwargs):
            return json.dumps({'items':[]}) if kwargs.get('capture') else None
        with patch.object(stack,'check_credentials'), patch.object(stack,'kubectl',side_effect=successful_call):
            stack.deploy()
        baseline = json.loads(stack.state_file.read_text())['applied_snapshot']
        self.document['backends']['vllm']['base_url'] = 'http://192.0.2.98:30810/v1'
        changed = self.stack(action='deploy')
        with patch.object(changed,'check_credentials'), patch.object(changed,'kubectl',side_effect=subprocess.CalledProcessError(1,['kubectl'])):
            with self.assertRaises(subprocess.CalledProcessError):
                changed.deploy()
        recorded = json.loads(changed.state_file.read_text())
        self.assertEqual(recorded['applied_snapshot'],baseline)
        self.assertIn('apply-failed',recorded['status'])

    def test_host_setting_changes_are_separate_from_gateway_restarts(self):
        stack = self.stack()
        self.rendered(stack)
        stack.state['applied_snapshot'] = stack.snapshot()
        stack.save()
        self.document['backends']['mlx-h3']['mlx']['max_concurrent'] = 2
        changed = self.stack(action='plan')
        output = io.StringIO()
        with redirect_stdout(output):
            changed.plan()
        plan = json.loads(output.getvalue())
        self.assertTrue(plan['host_configuration_changed'])
        self.assertEqual(plan['update_may_restart'],[])

    def test_release_import_preserves_bytes_and_rejects_mutable_images(self):
        source = ROOT/'contracts/release.yaml'
        destination = self.directory/'router.local.yaml'
        configure.import_release(source,destination)
        self.assertEqual(source.read_bytes(),destination.read_bytes())
        with self.assertRaises(FileExistsError):
            configure.import_release(source,destination)
        invalid = yaml.safe_load(source.read_text())
        invalid['images'][0]['reference'] = 'example/router:latest'
        candidate = self.directory/'bad-release.yaml'
        candidate.write_text(yaml.safe_dump(invalid))
        with self.assertRaisesRegex(ValueError,'immutable'):
            configure.import_release(candidate,self.directory/'never-written.yaml')
        self.assertFalse((self.directory/'never-written.yaml').exists())

    def test_migration_never_overwrites_existing_configuration(self):
        target = self.write()
        before = target.read_bytes()
        with self.assertRaisesRegex(ValueError,'already exists'):
            configure.migrate('hybrid','vllm-omni-cloud','environments/hybrid/catalog.yaml',target,'real')
        self.assertEqual(target.read_bytes(),before)

    def test_credentials_support_distinct_backend_keys_and_stale_detection(self):
        self.document['backends']['mlx-h3']['api_key_env'] = 'H3_API_KEY'
        stack = self.stack()
        values = {key:'synthetic-'+key for key in stack.config.required_keys}
        (self.directory/'secrets.env').write_text('\n'.join(k+'='+v for k,v in values.items()))
        with patch.object(credentials,'ROOT',self.root), patch.object(credentials,'apply_kubernetes') as apply:
            with redirect_stdout(io.StringIO()):
                credentials.initialize(SimpleNamespace(config=self.path,source_env=None))
            self.assertEqual(apply.call_args.args[1],values)
        stack.check_credentials()
        values['H3_API_KEY'] = 'rotated-private'
        (self.directory/'secrets.env').write_text('\n'.join(k+'='+v for k,v in values.items()))
        with self.assertRaisesRegex(ValueError,'stale'):
            stack.check_credentials()

    def test_mlx_launch_uses_generated_settings_and_never_puts_key_in_arguments(self):
        spec = importlib.util.spec_from_file_location('mlx_host_serve',ROOT/'tools/host-mlx/serve.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        settings = StackConfig(self.write()).mlx_settings('mlx-h3')
        model = self.directory/'model'
        model.mkdir()
        (model/'weight').write_bytes(b'weight')
        (model/'verification.json').write_text(json.dumps({'revision':settings['revision'],
                                                          'files':[{'file':'weight','bytes':6}]}))
        key = self.directory/'host-key'
        key.write_text('SENTINEL-PRIVATE')
        settings.update(model=str(model),secret_file=str(key),port=11235,max_concurrent=2)
        with patch.object(module.os,'execve') as execute:
            module.launch(settings)
        command = execute.call_args.args[1]
        self.assertNotIn('SENTINEL-PRIVATE',' '.join(command))
        self.assertEqual(command[command.index('--port')+1],'11235')
        self.assertEqual(execute.call_args.args[2]['MLX_STACK_API_KEY'],'SENTINEL-PRIVATE')

    def test_mock_configuration_cannot_start_real_mlx(self):
        self.document['mode'] = 'mock'
        with self.assertRaisesRegex(ValueError,'mode: real'):
            StackConfig(self.write()).mlx_settings('mlx-h3')


if __name__ == '__main__':
    unittest.main()
