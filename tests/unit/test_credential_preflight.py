"""Offline credential checks; no runtime calls or credential values in output."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import deploy


class CredentialPreflight(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.private = self.root / 'secrets/test'
        self.private.mkdir(parents=True)
        self.stack = deploy.Stack.__new__(deploy.Stack)
        self.stack.args = SimpleNamespace(environment='test')
        self.stack.mock = False
        self.stack.models = [{'api_key_env': 'CLOUD_API_KEY'}]
        self.patch = patch.object(deploy, 'ROOT', self.root)
        self.patch.start(); self.addCleanup(self.patch.stop)

    def credential(self, key, value='synthetic-private-fixture'):
        (self.private / key).write_text(value)

    def test_real_cloud_empty_key_rejected(self):
        self.credential('GATEWAY_TOKEN'); self.credential('CLOUD_API_KEY', ' \n')
        with self.assertRaisesRegex(ValueError, 'CLOUD_API_KEY'):
            self.stack.check_credentials()

    def test_real_cloud_does_not_require_unused_local_key(self):
        self.credential('GATEWAY_TOKEN'); self.credential('CLOUD_API_KEY')
        self.stack.check_credentials()

    def test_explicit_mock_can_omit_cloud_key(self):
        self.stack.mock = True
        self.credential('GATEWAY_TOKEN'); self.credential('MODEL_API_KEY')
        self.stack.check_credentials()

    def test_missing_gateway_rejected_without_revealing_value(self):
        self.credential('CLOUD_API_KEY', 'fixture-not-for-error-output')
        with self.assertRaises(ValueError) as captured:
            self.stack.check_credentials()
        self.assertIn('GATEWAY_TOKEN', str(captured.exception))
        self.assertNotIn('fixture-not-for-error-output', str(captured.exception))

    def test_local_backend_requires_its_credential(self):
        self.stack.models = [{'api_key_env': 'MODEL_API_KEY'}]
        self.credential('GATEWAY_TOKEN')
        with self.assertRaisesRegex(ValueError, 'MODEL_API_KEY'):
            self.stack.check_credentials()


if __name__ == '__main__':
    unittest.main()
