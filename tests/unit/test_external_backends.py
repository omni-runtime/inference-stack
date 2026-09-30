"""Cross-host deployments must not claim or manage another host's GPU."""
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"scripts"))
import deploy
import envoy

class ExternalBackends(unittest.TestCase):
    def stack(self, models, mock=False):
        env={"name":"unit-external", "runtime":"kubernetes", "platform":"linux/arm64", "release":"contracts/release.yaml", "resources":{"local_engines_concurrent":False}}
        args=SimpleNamespace(environment="unit-external",runtime="kubernetes",action="render",example="vllm-omni-cloud",overlay="mock" if mock else None,release=None,catalog=None)
        def read(path):
            name=Path(path).name
            if name=="environment.yaml":return env
            if name=="versions.lock.yml":return {"images":{"vllm":"locked","omni":"locked"}}
            if name=="release.yaml":return {"images":[]}
            if name=="example.yaml":return {"pools":["vllm","omni","cloud"]}
            return {"models":models}
        with patch.object(deploy,"read_yaml",side_effect=read),patch.object(deploy,"enforce_execution"):
            return deploy.Stack(args)
    def model(self,pool="vllm",service="text"):
        return {"name":"local/"+service,"pool":pool,"service":service,"base_url":"http://example.test:30810/v1","api_key_env":"MODEL_API_KEY","api_format":"openai","deployment":{"mode":"external"}}
    def test_external_pools_require_no_local_gpu_and_are_not_managed(self):
        s=self.stack([self.model(),self.model("omni","video")])
        s.check_resources()
        self.assertEqual(s.backends,{})
        self.assertEqual(s.enabled_services,["router","envoy"])
    def test_mock_external_backends_remain_mock_managed(self):
        s=self.stack([self.model()],True)
        self.assertIn("text",s.backends)
        self.assertEqual(s.models[0]["base_url"],"http://text:8000/v1")
    def test_external_rejects_embedded_credentials_and_managed_options(self):
        for change in [{"base_url":"http://secret@example.test/v1"},{"deployment":{"mode":"external","command":["bad"]}}]:
            m=self.model();m.update(change)
            with self.assertRaises(ValueError):self.stack([m])
    def test_video_timeout_is_per_selected_model(self):
        a=self.model();b=self.model("omni","video");b["request_timeout_seconds"]=3600
        c=envoy.render([a,b])["static_resources"]["listeners"][0]["filter_chains"][0]["filters"][0]["typed_config"]
        routes=c["route_config"]["virtual_hosts"][0]["routes"]
        self.assertEqual(routes[1]["route"]["timeout"],"120s")
        self.assertEqual(routes[2]["route"]["idle_timeout"],"3600s")
        b["request_timeout_seconds"]=0
        with self.assertRaises(ValueError):envoy.render([b])

if __name__=="__main__":unittest.main()
