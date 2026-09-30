"""Operator stop intent survives API/wait failures; no cluster calls."""
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import deploy


class KubernetesStop(unittest.TestCase):
    def stack(self, action='stop'):
        stack = deploy.Stack.__new__(deploy.Stack)
        stack.args = SimpleNamespace(action=action, service='vllm')
        stack.enabled_services = ['vllm', 'omni']
        stack.backends = {'vllm': {'replicas': 1}}
        stack.runtime = 'kubernetes'
        stack.state = {'stopped': ['omni']}
        stack.save = Mock()
        stack.kubectl = Mock()
        return stack

    def test_scale_failure_retains_operator_stop_intent(self):
        stack = self.stack()
        stack.kubectl.side_effect = subprocess.CalledProcessError(1, ['kubectl', 'scale'])
        with self.assertRaises(subprocess.CalledProcessError):
            stack.operate()
        self.assertEqual(stack.state['stopped'], ['omni', 'vllm'])
        stack.save.assert_called_once()

    def test_stop_waits_for_pod_deletion_after_scaling(self):
        stack = self.stack()
        stack.operate()
        calls = [c.args for c in stack.kubectl.call_args_list]
        self.assertEqual(calls[0][:3], ('scale', 'deployment/vllm', '--replicas=0'))
        self.assertEqual(calls[-1], ('wait', '--for=delete', 'pod', '-l', 'app=vllm', '--timeout=120s'))
        self.assertEqual(stack.state['stopped'], ['omni', 'vllm'])

    def test_failed_start_does_not_clear_stop_record(self):
        stack = self.stack('start')
        stack.state['stopped'].append('vllm')
        stack.kubectl.side_effect = [None, subprocess.CalledProcessError(1, ['kubectl', 'rollout'])]
        with self.assertRaises(subprocess.CalledProcessError):
            stack.operate()
        self.assertIn('vllm', stack.state['stopped'])
        stack.save.assert_not_called()


if __name__ == '__main__':
    unittest.main()
