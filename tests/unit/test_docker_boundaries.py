"""Portable Docker policy must retain serving-container isolation."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from boundary import BoundaryError, enforce_execution, validate_compose


class DockerBoundaries(unittest.TestCase):
    def setUp(self):
        self.env = {'name': 'custom', 'runtime': 'docker', 'project': 'demo',
                    'docker': {'allowed_readonly_model_source': '/srv/models',
                               'volumes': {x: 'demo-' + x for x in
                                           ['config', 'credentials', 'cache', 'outputs', 'media_bindings']}}}
        self.config = {'name': 'demo', 'volumes': {'config': {'name': 'demo-config'}},
                       'services': {'model': {'volumes': [
                           {'type': 'bind', 'source': '/srv/models', 'target': '/models',
                            'read_only': True, 'bind': {'create_host_path': False}},
                           {'type': 'volume', 'source': 'config', 'target': '/config', 'read_only': True}]}}}

    def test_operator_selected_environment_needs_no_historical_host_or_window(self):
        enforce_execution(self.env)
        validate_compose(self.config, self.env)

    def test_readonly_mount_allowlist_cannot_be_bypassed_by_service(self):
        for change in [{'source': '/'}, {'read_only': False},
                       {'bind': {'create_host_path': True}}, {'target': '/var/run/docker.sock'}]:
            with self.subTest(change=change):
                config = copy.deepcopy(self.config)
                config['services']['model']['volumes'][0].update(change)
                with self.assertRaises(BoundaryError):
                    validate_compose(config, self.env)

    def test_host_privileges_and_backend_ports_remain_rejected(self):
        for key, value in [('privileged', True), ('network_mode', 'host'),
                           ('devices', ['/dev/nvidia0']), ('ports', ['8000:8000'])]:
            with self.subTest(key=key):
                config = copy.deepcopy(self.config)
                config['services']['model'][key] = value
                with self.assertRaises(BoundaryError):
                    validate_compose(config, self.env)

    def test_serving_configuration_remains_readonly(self):
        self.config['services']['model']['volumes'][1]['read_only'] = False
        with self.assertRaises(BoundaryError):
            validate_compose(self.config, self.env)

    def test_optional_operator_window_requires_timezone_and_is_enforced(self):
        for window in ['2999-01-01T00:00:00+00:00', '2020-01-01T00:00:00']:
            with self.subTest(window=window), self.assertRaises(BoundaryError):
                enforce_execution({**self.env, 'verification_not_before': window})
