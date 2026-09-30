#!/usr/bin/env python3
"""Verify native task binding across SR/Redis restarts and explicit failures."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import tempfile
import time

import yaml

from run import Run


class VideoLifecycle(Run):
    def execute(self):
        if self.args.runtime != "kubernetes":
            raise ValueError("this lifecycle suite controls Kubernetes deployments explicitly")
        self.wait_gateway()
        operations = []
        config = json.loads(self.run_command("get", "configmap/router-config", "-o", "json"))["data"]["router.yaml"]
        pvc = json.loads(self.run_command("get", "pvc/media-bindings", "-o", "json"))["metadata"]["uid"]

        def operation(*parts):
            value = self.run_command(*parts)
            operations.append({"time":time.time(), "command":list(parts), "output":value})
            (self.output/"operations.json").write_text(json.dumps(operations,indent=2)+"\n")
            return value

        def patch_router(value):
            with tempfile.NamedTemporaryFile(mode="w",dir=self.output,suffix=".json") as patch:
                json.dump({"data":{"router.yaml":value}},patch)
                patch.flush()
                subprocess.run(self.command+["patch","configmap/router-config","--type=merge","--patch-file",patch.name],
                    text=True,check=True,stdout=subprocess.DEVNULL)
            operation("rollout","restart","deployment/router")
            operation("rollout","status","deployment/router","--timeout=60s")
            self.wait_gateway()

        models = [m for m in self.models if m["api_format"]=="video" and m["pool"]=="omni"]
        body = self.multipart_call("video-lifecycle-create","/v1/videos",[("model","local-only"),("prompt","A blue square")],[],[200],
                                   [m["service"] for m in models],kind="video-job")
        if self.records[-1]["error"]:
            return self.collect()
        task_id = json.loads(body)["id"]
        self.records[-1]["public_video_id"] = task_id
        backend = self.records[-1]["response_headers"]["x-mock-backend"]
        path = "/v1/videos/"+task_id

        def query(name, statuses=(200,), reaches_backend=True):
            data = self.call(name,path,{},list(statuses),[backend] if reaches_backend else [],method="GET",
                             kind="video-job" if statuses==(200,) else "service-unavailable",
                             headers={"X-Selected-Model":"cloud/video-async","X-VSR-Skip-Processing":"true"})
            self.records[-1]["public_video_id"] = task_id
            if statuses==(200,) and not self.records[-1]["error"] and json.loads(data)["id"]!=task_id:
                self.records[-1]["error"]="public task identity changed after restart"

        try:
            operation("scale","deployment/router","--replicas=2")
            operation("rollout","status","deployment/router","--timeout=60s")
            operation("rollout","restart","deployment/router")
            operation("rollout","status","deployment/router","--timeout=60s")
            self.wait_gateway()
            query("video-restored-by-new-router-generation")
            operation("scale","deployment/media-bindings","--replicas=0")
            operation("wait","--for=delete","pod","-l","app=media-bindings","--timeout=60s")
            query("video-binding-store-down",(503,),False)
            self.multipart_call("video-create-store-down","/v1/videos",[("model","local-only"),("prompt","Must not execute")],[],
                                [503],[],kind="service-unavailable")
            operation("scale","deployment/media-bindings","--replicas=1")
            operation("rollout","status","deployment/media-bindings","--timeout=60s")
            query("video-restored-after-redis-aof-restart")
            # Interrupt only the state commit after one confirmed backend
            # creation. The gateway must not return a false success or replay.
            with ThreadPoolExecutor(max_workers=1) as executor:
                started = time.time()
                pending = executor.submit(self.multipart_call,"video-created-binding-commit-failure","/v1/videos",
                    [("model","local-only"),("prompt","fixture:slow-task-response")],[],[503],[backend])
                deadline = time.monotonic()+10
                while True:
                    logs = self.run_command("logs","deployment/"+backend,"--tail=100")
                    events = [json.loads(line) for line in logs.splitlines() if line.startswith("{")]
                    if any(e.get("event")=="video_created" and e["time"]>=started for e in events):
                        break
                    if time.monotonic()>=deadline:
                        raise RuntimeError("delayed task did not reach its backend")
                    time.sleep(0.1)
                operation("scale","deployment/media-bindings","--replicas=0")
                operation("wait","--for=delete","pod","-l","app=media-bindings","--timeout=60s")
                error_body = pending.result(timeout=40)
                if b"created but its binding could not be saved" not in error_body:
                    self.records[-1]["error"] = "creation uncertainty was not reported explicitly"
            operation("scale","deployment/media-bindings","--replicas=1")
            operation("rollout","status","deployment/media-bindings","--timeout=60s")
            query("video-existing-binding-survives-unrelated-commit-failure")
            document = yaml.safe_load(config)
            instances = document["global"]["stores"]["media_bindings"]["instances"]
            for model in models:
                instances[model["name"]]+="-changed"
            patch_router(yaml.safe_dump(document,sort_keys=False))
            query("video-rejects-changed-instance",(409,),False)
            patch_router(config)
            query("video-restored-after-explicit-config-rollback")
            script = "for _, k in ipairs(redis.call('KEYS','vsr:media-binding:v1:entry:*')) do local v=cjson.decode(redis.call('GET',k)); if v.key==ARGV[1] then return redis.call('PEXPIRE',k,1) end end; return 0"
            assert operation("exec","deployment/media-bindings","--","redis-cli","EVAL",script,"0",task_id).strip()=="1"
            query("video-expired-binding",(404,),False)
            assert json.loads(self.run_command("get","pvc/media-bindings","-o","json"))["metadata"]["uid"]==pvc
        finally:
            try:
                patch_router(config)
            finally:
                operation("scale","deployment/media-bindings","--replicas=1")
                operation("scale","deployment/router","--replicas=1")
                operation("rollout","status","deployment/router","--timeout=60s")
        return self.collect()


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment",default="kubernetes")
    parser.add_argument("--runtime",default="kubernetes")
    parser.add_argument("--example",default="vllm-omni-cloud")
    parser.add_argument("--overlay",default="mock")
    parser.add_argument("--catalog",default="tests/overlays/mock/catalog-media.yaml")
    parser.add_argument("--url")
    raise SystemExit(1 if VideoLifecycle(parser.parse_args()).execute() else 0)
