"""Concurrent callbacks must remain independently parseable native evidence."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import time
import unittest


class SlowOutput(io.StringIO):
    def write(self, value):
        result = super().write(value)
        # Give another callback time to write between JSON and its newline.
        if value != '\n':
            time.sleep(0.001)
        return result


class MockEvidenceTest(unittest.TestCase):
    def test_concurrent_records_remain_json_lines(self):
        source = Path(__file__).resolve().parents[1]/'overlays/mock/mock_backend.py'
        spec = importlib.util.spec_from_file_location('mock_backend_evidence', source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        output = SlowOutput()
        records = [{'event': 'stream_finished', 'request_id': str(i)} for i in range(80)]
        with redirect_stdout(output), ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(module.evidence, records))
        observed = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertCountEqual(observed, records)
        self.assertEqual(observed, module.EVIDENCE)


if __name__ == '__main__':
    unittest.main()
