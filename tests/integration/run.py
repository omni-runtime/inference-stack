#!/usr/bin/env python3
"""Exercise the real gateway and correlate responses with native upstream logs.

The backend overlay must be selected explicitly. No fixture emulates SR or
reimplements selection; expected sets express hard policy/task constraints.
Reports contain hashes and native evidence, not credentials or request bodies.
"""
import argparse
import base64
import struct
import zlib
import hashlib
import http.client
import io
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import wave

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from boundary import enforce_execution


class Run:
    def __init__(self, args):
        self.args = args
        self.environment = yaml.safe_load((ROOT / "environments" / args.environment / "environment.yaml").read_text())
        if self.environment["runtime"] != args.runtime:
            raise ValueError("environment/runtime mismatch")
        enforce_execution(self.environment)
        self.pools = yaml.safe_load((ROOT / "examples" / args.example / "example.yaml").read_text())["pools"]
        catalog = yaml.safe_load((ROOT / args.catalog).read_text())
        if catalog.get("mock_only") and args.overlay != "mock":
            raise ValueError("mock model catalog requires explicit mock overlay")
        self.models = [m for m in catalog["models"] if m["pool"] in self.pools]
        self.backend_services = sorted({m["service"] for m in self.models if args.overlay == "mock" or (m["pool"] != "cloud" and m.get("deployment", {}).get("mode") != "external")})
        self.records = []
        self.readiness = []
        self.started_at = time.time()
        if args.runtime == "kubernetes":
            k = self.environment["kubernetes"]
            self.command = ["kubectl", "--kubeconfig", k["kubeconfig"], "--context", k["context"], "-n", k["namespace"]]
            self.url = args.url or f'http://{k["ssh"].split("@")[-1]}:{k["gateway_node_port"]}'
        else:
            d = self.environment["docker"]
            self.command = ["docker", "--context", d["context"], "compose", "-p", d["project"], "-f",
                            str(ROOT / "generated" / args.environment / args.example / "compose.yaml")]
            self.url = args.url or "http://envoy:8000"
        self.token = (ROOT / "secrets" / args.environment / "GATEWAY_TOKEN").read_text().strip()
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        self.output = ROOT / "reports" / args.environment / args.runtime / (stamp + "-" + args.example)
        self.output.mkdir(parents=True, exist_ok=False)

    def wait_gateway(self):
        deadline = time.monotonic() + 30
        healthy_since = None
        while True:
            request_id = str(uuid.uuid4())
            attempt = {"request_id":request_id,"status":0,"path":"/v1/models"}
            try:
                request = urllib.request.Request(self.url + "/v1/models",headers={"Authorization":"Bearer "+self.token,"X-Request-ID":request_id})
                with urllib.request.urlopen(request, timeout=2) as response:
                    attempt["status"] = response.status
                    body = response.read(1048576)
                    attempt["response_sha256"] = hashlib.sha256(body).hexdigest()
                    if response.status == 200 and isinstance(json.loads(body).get("data"),list):
                        self.readiness.append(attempt)
                        now = time.monotonic()
                        if healthy_since is None:
                            healthy_since = now
                        if now-healthy_since >= 2:
                            return
                        time.sleep(0.1)
                        continue
            except (urllib.error.URLError, TimeoutError, ValueError) as error:
                attempt["status"] = getattr(error,"code",0)
            healthy_since = None
            self.readiness.append(attempt)
            if time.monotonic() >= deadline:
                raise RuntimeError("gateway plus SR protocol readiness did not become reachable")
            time.sleep(0.25)

    def run_command(self, *parts):
        return subprocess.check_output(self.command + list(parts), text=True, stderr=subprocess.STDOUT, timeout=60)

    def call(self, case, path, payload, statuses, backends=(), *, content_type="application/json", headers=None, token=True, kind=None, stream_mode=None, raw_body=None, method="POST", timeout=40):
        request_id = str(uuid.uuid4())
        body = raw_body if raw_body is not None else json.dumps(payload).encode()
        request_headers = {"Content-Type":content_type, "X-Request-ID":request_id}
        if token:
            request_headers["Authorization"] = "Bearer " + self.token
        request_headers.update(headers or {})
        if method in {"GET", "DELETE"}:
            body = b""
            request_headers.pop("Content-Type", None)
        request = urllib.request.Request(self.url + path, body if method == "POST" else None, request_headers, method=method)
        start = time.monotonic()
        error = None
        try:
            try:
                response = urllib.request.urlopen(request, timeout=timeout)
            except urllib.error.HTTPError as failure:
                response = failure
            with response:
                first_byte_ms = None
                if stream_mode == "cancel" and response.status == 200:
                    data = response.readline(65536)
                    first_byte_ms = round((time.monotonic()-start)*1000, 2)
                    assert b"data:" in data and first_byte_ms < 2000, "stream was buffered instead of delivered incrementally"
                elif stream_mode == "interrupt" and response.status == 200:
                    chunks = []
                    total = 0
                    try:
                        while total <= 16 * 1024 * 1024:
                            chunk = response.read1(65536)
                            if not chunk:
                                break
                            chunks.append(chunk)
                            total += len(chunk)
                    except http.client.IncompleteRead as interrupted:
                        chunks.append(interrupted.partial)
                    data = b"".join(chunks)
                elif kind == "mlx-sse" and response.status == 200:
                    first = response.readline(65536)
                    first_byte_ms = round((time.monotonic()-start)*1000, 2)
                    data = first + response.read(16 * 1024 * 1024 + 1)
                else:
                    data = response.read(16 * 1024 * 1024 + 1)
                status = response.status
                response_headers = {k.lower():v for k,v in response.headers.items()
                                    if k.lower().startswith("x-vsr-") or k.lower() in {"content-type", "x-request-id", "x-mock-backend", "content-disposition"}}
            if len(data) > 16 * 1024 * 1024:
                raise AssertionError("response exceeded fixture limit")
            if status not in statuses:
                # Backend/SR errors in these tests contain no submitted secrets.
                raise AssertionError(f"status {status}; expected {statuses}; error {data[:500]!r}")
            if status == 503 and not backends and kind != "service-unavailable":
                # The locked SR maps ErrNoEligibleCandidates to this native
                # error. An Envoy connection failure is not equivalent.
                assert json.loads(data)["error"]["code"] == "upstream_unavailable"
                assert response_headers.get("x-vsr-response-path") == "error"
            if status == 200 and stream_mode:
                assert b"data:" in data and b"[DONE]" not in data
            if status == 200 and kind in {"mlx-json", "mlx-sse"}:
                if kind == "mlx-sse":
                    assert response_headers.get("content-type", "").startswith("text/event-stream")
                    events = [json.loads(line[6:]) for line in data.splitlines() if line.startswith(b"data: ")]
                    assert events[0]["type"] == "progress" and events[-1]["type"] == "complete"
                    result = events[-1]
                else:
                    result = json.loads(data)
                assert result["format"] == "rgb8" and result["audio_format"] == "pcm_s16le"
                rgb = base64.b64decode(result["data"], validate=True)
                assert len(rgb) == result["frames"]*result["width"]*result["height"]*3
                pcm = base64.b64decode(result["audio_data"], validate=True)
                assert len(pcm) > 0 and len(pcm) % (2*result["audio_channels"]) == 0
            if status == 200 and kind == "wav":
                with wave.open(io.BytesIO(data)) as audio:
                    assert audio.getnframes() > 0 and audio.getframerate() > 0
                    audio_metadata = {"frames":audio.getnframes(), "sample_rate":audio.getframerate(),
                                      "channels":audio.getnchannels(), "sample_width":audio.getsampwidth()}
            if status == 200 and kind in {"sse", "speech-sse"}:
                assert response_headers.get("content-type", "").startswith("text/event-stream")
                assert b"data:" in data and (b"[DONE]" in data or b"speech.audio.done" in data)
            if status == 200 and kind == "speech-sse":
                events = [json.loads(line[6:]) for line in data.splitlines() if line.startswith(b"data: ")]
                assert events and events[-1]["type"] == "speech.audio.done"
                assert all(e["type"] in {"speech.audio.delta", "speech.audio.done"} for e in events)
                deltas = [e for e in events if e["type"] == "speech.audio.delta"]
                assert deltas and all(e["response_format"] == "pcm" for e in deltas)
                chunks = [base64.b64decode(e["audio"], validate=True) for e in deltas]
                assert all(chunk and len(chunk)%2 == 0 for chunk in chunks)
                usage = events[-1]["usage"]
                assert usage["input_tokens"] > 0 and usage["output_tokens"] > 0
                audio_metadata = {"deltas":len(chunks), "pcm_bytes":sum(map(len,chunks)),
                                  "pcm_sha256":hashlib.sha256(b"".join(chunks)).hexdigest(), "usage":usage}
            if status == 200 and kind in {"image-b64", "image-file", "image-url"}:
                if kind == "image-url":
                    assert json.loads(data)["data"][0]["url"] == "https://example.invalid/mock-image.png"
                else:
                    png = data if kind == "image-file" else base64.b64decode(json.loads(data)["data"][0]["b64_json"], validate=True)
                    assert png[:8] == b"\x89PNG\r\n\x1a\n"
                    assert struct.unpack(">II", png[16:24]) == (2, 2)
                    position = 8
                    while position < len(png):
                        size = struct.unpack(">I", png[position:position+4])[0]
                        chunk = png[position+4:position+8+size]
                        assert zlib.crc32(chunk) == struct.unpack(">I", png[position+8+size:position+12+size])[0]
                        position += 12 + size
                    assert position == len(png)
                if kind == "image-file":
                    assert response_headers.get("content-type", "").startswith("image/png")
                    assert "fixture.png" in response_headers.get("content-disposition", "")
            if status == 200 and kind == "asr-json":
                assert json.loads(data)["text"] == "mock transcript"
            if status == 200 and kind == "video-file":
                assert response_headers.get("content-type", "").startswith("video/mp4")
                assert "fixture.mp4" in response_headers.get("content-disposition", "")
                assert data == (ROOT/"tests/requests/assets/blue.mp4").read_bytes()
            if case == "video-backend-error" and status == 422:
                assert json.loads(data)["error"]["code"] == "fixture_video_error"
            if status == 200 and kind == "video-job":
                assert json.loads(data)["id"].startswith("video_sr_")
            if status == 200 and kind == "asr-text":
                assert b"mock transcript" in data and response_headers.get("content-type", "").startswith("text/")
            if case == "asr-backend-error" and status == 422:
                assert json.loads(data)["error"]["code"] == "fixture_asr_error"
            if status == 200 and kind in {"chat-audio", "chat-audio-sse"}:
                chunks = [json.loads(line[6:]) for line in data.splitlines() if line.startswith(b"data: ") and line!=b"data: [DONE]"] if kind.endswith("sse") else [json.loads(data)]
                choice = chunks[0]["choices"][0]
                message = choice["delta" if kind.endswith("sse") else "message"]
                assert message["audio"]["id"] == "audio-mock" and message["audio"]["transcript"].startswith("mock response")
                assert choice["audio_metadata"]["sample_rate"] == 24000
                if "text" in payload["modalities"]:
                    assert message["content"].startswith("mock response")
                with wave.open(io.BytesIO(base64.b64decode(message["audio"]["data"],validate=True))) as audio:
                    assert audio.getnframes() == 2400 and audio.getframerate() == 24000
                if kind.endswith("sse"):
                    assert b"data: [DONE]" in data
            if status == 200 and kind == "tools":
                tool = json.loads(data)["choices"][0]["message"]["tool_calls"][0]
                assert tool["function"]["name"] == "record_color"
                assert json.loads(tool["function"]["arguments"]) == {"color":"blue"}
            if status == 200 and kind == "structured":
                assert json.loads(json.loads(data)["choices"][0]["message"]["content"]) == {"color":"blue"}
            if case == "images-backend-error" and status == 422:
                assert json.loads(data)["error"]["code"] == "fixture_image_error"
            if status == 200 and kind == "chat":
                parsed = json.loads(data)
                chat_metadata = {"usage":parsed.get("usage"), "choices":[{
                    "finish_reason":c.get("finish_reason"),
                    "content_characters":len(c.get("message", {}).get("content") or ""),
                    "reasoning_characters":len(c.get("message", {}).get("reasoning_content") or "")}
                    for c in parsed.get("choices", [])]}
                assert parsed["choices"][0]["message"]["content"], "chat response contains no final text"

        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            status = locals().get("status", 0)
            data = locals().get("data", b"")
            response_headers = locals().get("response_headers", {})
        self.records.append({"case":case, "request_id":request_id, "client_path":path,
            "status":status, "expected_statuses":statuses, "expected_backend_set":list(backends),
            "duration_ms":round((time.monotonic()-start)*1000, 2), "response_headers":response_headers,
            "request_sha256":hashlib.sha256(body).hexdigest(), "response_sha256":hashlib.sha256(data).hexdigest(),
            "response_bytes":len(data), "error":error, "chat_metadata":locals().get("chat_metadata"), "audio_metadata":locals().get("audio_metadata"), "stream_mode":stream_mode, "first_byte_ms":locals().get("first_byte_ms"),
            "chat_output_sha256": (hashlib.sha256(json.dumps({k:payload[k] for k in ("modalities","audio") if k in payload},sort_keys=True).encode()).hexdigest() if "modalities" in payload else None),
            "native_output": "audio" in payload.get("modalities", []) or path in {"/v1/videos/sync", "/v1/video/generations"} or path.endswith("/content"),
            "messages_sha256": (hashlib.sha256(json.dumps(payload["messages"],sort_keys=True).encode()).hexdigest() if case.startswith("chat-media-") else None),
            "audio_parameters_sha256": (hashlib.sha256(json.dumps({k:v for k,v in payload.items() if k != "model"},sort_keys=True).encode()).hexdigest() if path == "/v1/audio/generate" else None),
            "video_parameters_sha256": (hashlib.sha256(json.dumps({k:v for k,v in payload.items() if k != "model"},sort_keys=True).encode()).hexdigest() if path == "/v1/video/generations" else None),
            "image_parameters_sha256": (hashlib.sha256(json.dumps({k:v for k,v in payload.items() if k != "model"}, sort_keys=True).encode()).hexdigest() if path == "/v1/images/generations" else None)})
        print(f"{case}: {status}" + (f" FAIL {error}" if error else ""), flush=True)
        return data

    def video_tasks(self):
        models = [m for m in self.models if m["api_format"] == "video"]
        if not models:
            return
        for scope,pools in [("auto",set(self.pools)),("local-only",{"omni","vllm"}),("cloud-only",{"cloud"})]:
            allowed = [m["service"] for m in models if m["pool"] in pools]
            fields = [("model",scope),("prompt","Generate a blue square"),("seconds","1")]
            data = self.multipart_call("video-create-"+scope,"/v1/videos",fields,[],[200] if allowed else ([503] if set(self.pools)&pools else [400]),allowed,kind="video-job")
            if not allowed or self.records[-1]["error"]:
                continue
            task = json.loads(data)
            task_id = task["id"]
            if task.get("status") != "queued" or not task_id.startswith("video_sr_"):
                self.records[-1]["error"] = "invalid asynchronous task response"
                continue
            backend = self.records[-1]["response_headers"]["x-mock-backend"]
            self.records[-1]["public_video_id"] = task_id
            path = "/v1/videos/"+task_id
            for operation,method,suffix,statuses,kind in [("query","GET","",[200],"video-job"),
                    ("download","GET","/content",[200],"video-file"),("delete","DELETE","",[200],"video-job"),
                    ("after-delete","GET","",[404],None)]:
                response = self.call("video-"+scope+"-"+operation,path+suffix,{},statuses,[backend],method=method,kind=kind,
                    headers={"X-Selected-Model":"forged/other-backend","X-VSR-Skip-Processing":"true"})
                self.records[-1]["public_video_id"] = task_id
                if statuses == [200] and not suffix and not self.records[-1]["error"]:
                    if json.loads(response)["id"] != task_id:
                        self.records[-1]["error"] = "public task id changed on continuation"
        allowed = [m["service"] for m in models]
        self.multipart_call("video-invalid-provider-response","/v1/videos",[("model","auto"),("prompt","fixture:invalid-task-response")],[],[502],allowed)
        self.multipart_call("video-async-backend-error","/v1/videos",[("model","auto"),("prompt","fixture:backend-error")],[],[422],allowed)
        self.call("video-unknown-id","/v1/videos/video_sr_00000000-0000-4000-8000-000000000001",{},[404],method="GET")
        self.call("video-list-rejected","/v1/videos",{},[405],method="GET")

    def multipart_call(self, case, path, fields, files, statuses, backends=(), *, kind=None, content_type=None):
        boundary = "fixture-"+uuid.uuid4().hex
        chunks = []
        form_values = {}
        for name,value in fields:
            form_values.setdefault(name, []).append(value)
            chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode())
        expected_files = []
        for name,filename,mime_type,data in files:
            chunks += [(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\nContent-Type: {mime_type}\r\n\r\n').encode(),data,b"\r\n"]
            expected_files.append({"name":name,"filename":filename,"content_type":mime_type,"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest()})
        chunks.append((f'--{boundary}--\r\n').encode())
        data = self.call(case,path,{},statuses,backends,kind=kind,content_type=content_type or "multipart/form-data; boundary="+boundary,raw_body=b"".join(chunks))
        self.records[-1]["form_sha256"] = hashlib.sha256(json.dumps({k:v for k,v in form_values.items() if k!="model"},sort_keys=True).encode()).hexdigest()
        self.records[-1]["expected_files"] = expected_files
        return data

    def collect(self):
        (self.output / "client-records.json").write_text(json.dumps(self.records, indent=2) + "\n")
        native = {}
        for service in ["envoy", "router"] + self.backend_services:
            try:
                if self.args.runtime == "kubernetes":
                    logs = self.run_command("logs", "deployment/"+service, "--tail=3000")
                else:
                    logs = self.run_command("logs", "--no-color", "--no-log-prefix", "--tail=3000", service)
                # Router diagnostics may contain request content. Preserve only
                # structured Envoy/mock records, never dump arbitrary model logs.
                native[service] = []
                for line in logs.splitlines():
                    try:
                        value = json.loads(line)
                        if isinstance(value, dict) and "request_id" in value:
                            native[service].append(value)
                    except ValueError:
                        pass
            except subprocess.CalledProcessError:
                native[service] = []
        # Envoy's access log flush is asynchronous. Poll for evidence without
        # retrying any inference request or accepting an unobserved destination.
        deadline = time.monotonic() + 10
        expected = {r["request_id"] for r in self.records}
        while not expected <= {r.get("request_id") for r in native.get("envoy", [])} and time.monotonic() < deadline:
            time.sleep(0.25)
            if self.args.runtime == "kubernetes":
                logs = self.run_command("logs", "deployment/envoy", "--tail=3000")
            else:
                logs = self.run_command("logs", "--no-color", "--no-log-prefix", "--tail=3000", "envoy")
            native["envoy"] = []
            for line in logs.splitlines():
                try:
                    value = json.loads(line)
                    if isinstance(value, dict) and "request_id" in value:
                        native["envoy"].append(value)
                except ValueError:
                    pass
        # Wait for backend cancellation/interruption evidence without replay.
        expected_streams = {r["request_id"] for r in self.records if r.get("stream_mode") or r.get("websocket_frames")}
        deadline = time.monotonic() + 5
        while not expected_streams <= {r.get("request_id") for entries in native.values() for r in entries if r.get("event") in ("stream_finished", "websocket_closed")}:
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
            for service in self.backend_services:
                logs = (self.run_command("logs", "deployment/"+service, "--tail=3000") if self.args.runtime == "kubernetes" else
                        self.run_command("logs", "--no-color", "--no-log-prefix", "--tail=3000", service))
                values = []
                for line in logs.splitlines():
                    try:
                        value = json.loads(line)
                        if isinstance(value, dict) and "request_id" in value:
                            values.append(value)
                    except ValueError:
                        pass
                native[service] = values
        for record in self.records:
            rid = record["request_id"]
            upstream = [entry for entry in native.get("envoy", []) if entry.get("request_id") == rid]
            received = [entry for service, entries in native.items() if service not in {"router", "envoy"}
                        for entry in entries if entry.get("request_id") == rid and "path" in entry]
            record["envoy_upstream_evidence"] = upstream
            record["backend_evidence"] = received
            failures = []
            if not upstream:
                failures.append("native Envoy request evidence missing")
            if self.args.overlay == "mock":
                allowed = record["expected_backend_set"]
                if allowed and record["status"] in record["expected_statuses"]:
                    if len(received) != 1 or received[0].get("backend") not in allowed:
                        failures.append("actual backend evidence outside expected set or not exactly one call")
                elif not allowed and received:
                    failures.append("request expected rejection but reached a backend")
            if record.get("stream_mode"):
                events = [e for entries in native.values() for e in entries if e.get("request_id") == rid and e.get("event") == "stream_finished"]
                record["stream_evidence"] = events
                expected = "client_cancelled" if record["stream_mode"] == "cancel" else "backend_interrupted"
                if len(events) != 1 or events[0].get("outcome") != expected:
                    failures.append("stream lifecycle evidence missing or incorrect")
                elif record["stream_mode"] == "cancel" and events[0].get("chunks_sent", 100) >= 30:
                    failures.append("backend continued producing the full response after cancellation")
            if record.get("websocket_frames"):
                proof = record["websocket_frames"]
                from websocket_cases import headers_only_extproc
                if len(upstream)!=1 or not headers_only_extproc(upstream[0]):
                    failures.append("WebSocket header-only ExtProc evidence missing or body calls present")
                events = [e for service,entries in native.items() if service not in {"router","envoy"} for e in entries if e.get("request_id")==rid and isinstance(e.get("event"),str) and e["event"].startswith("websocket_")]
                record["websocket_evidence"] = events
                fields = ("opcode","sha256","bytes")
                client_sent = [{k:e[k] for k in fields} for e in events if e["event"]=="websocket_received"]
                server_sent = [{k:e[k] for k in fields} for e in events if e["event"]=="websocket_sent"]
                if client_sent != proof["sent"]:
                    failures.append("client native frames changed or reached another session")
                if server_sent[:len(proof["received"])] != proof["received"]:
                    failures.append("server native frames changed")
                closes = [e for e in events if e["event"]=="websocket_closed"]
                want = "backend_disconnected" if proof["behavior"]=="disconnect" else "peer_disconnected" if proof["behavior"]=="slow" else "session_close" if proof["speech"] else "client_close"
                if len(closes)!=1 or closes[0].get("outcome")!=want:
                    failures.append("native WebSocket close outcome missing or incorrect")
                if proof["behavior"]=="slow" and len(server_sent)>=30:
                    failures.append("native generation continued after client disconnect")
                if received and received[0].get("provider_model")!=record.get("provider_model"):
                    failures.append("frame model differs from selected handshake model")
            for field in ["messages_sha256", "audio_parameters_sha256", "video_parameters_sha256", "form_sha256", "chat_output_sha256"]:
                if received and record.get(field) and received[0].get(field) != record[field]:
                    failures.append(field + " changed between ingress and backend")
            if received and record.get("native_output") and record["status"] == 200 and not record.get("stream_mode"):
                sent = [e for entries in native.values() for e in entries if e.get("request_id")==rid and e.get("event") in ("response_sent","stream_finished")]
                if len(sent)!=1 or sent[0].get("response_sha256") != record["response_sha256"]:
                    failures.append("native mixed response bytes changed or missing backend response evidence")
            if received and "expected_files" in record:
                if sorted(received[0].get("files", []), key=lambda f:(f["name"],f["filename"])) != sorted(record["expected_files"], key=lambda f:(f["name"],f["filename"])):
                    failures.append("multipart file bytes, filenames or MIME types changed")
            if received and record.get("public_video_id") and record["client_path"] != "/v1/videos":
                created = [e for entries in native.values() for e in entries if e.get("event") == "video_created"
                           and e.get("request_id") in {c["request_id"] for c in self.records if c.get("public_video_id") == record["public_video_id"] and c["client_path"] == "/v1/videos"}]
                if len(created) != 1 or received[0]["path"] != record["client_path"].replace(record["public_video_id"], created[0]["provider_video_id"]):
                    failures.append("task continuation lost the original provider task id")
            if self.args.overlay == "real":
                allowed = record["expected_backend_set"]
                actual = [entry for entry in upstream if entry.get("upstream_host")]
                if allowed:
                    eligible_models = [m["name"] for m in self.models if m["service"] in allowed]
                    if len(actual) != 1 or actual[0].get("selected_model") not in eligible_models:
                        failures.append("real request lacks the expected native upstream host/model evidence")
                    elif actual[0].get("status") != record["status"]:
                        failures.append("native upstream status differs from client status")
                elif actual:
                    failures.append("rejected request reached a real upstream")
                if any(r.get("response_headers", {}).get("x-mock-backend") for r in [record]):
                    failures.append("real-backend test unexpectedly reached a mock")
            if received and any(not entry.get("authorization_matches") for entry in received):
                failures.append("provider credential was not applied")
            if received and record.get("image_parameters_sha256"):
                if received[0].get("image_parameters_sha256") != record["image_parameters_sha256"]:
                    failures.append("image generation controls changed between ingress and backend")
            if failures:
                record["error"] = "; ".join(filter(None, [record["error"], *failures]))
        if self.args.runtime == "kubernetes":
            inventory = json.loads(self.run_command("get", "pods,deployments,services,pvc", "-l", "app.kubernetes.io/part-of=inference-stack", "-o", "json"))
        else:
            inventory = self.run_command("ps", "--all", "--format", "json")
        (self.output / "resources.json").write_text(json.dumps(inventory, indent=2) + "\n")
        expected_ids = {r["request_id"] for r in self.records}
        unexpected_calls = [entry for service, entries in native.items() if service not in {"router", "envoy"}
                            for entry in entries if "path" in entry and entry.get("time", 0) >= self.started_at
                            and entry.get("request_id") not in expected_ids]
        report = {"environment":self.args.environment, "runtime":self.args.runtime, "example":self.args.example,
            "backend_type":self.args.overlay, "gateway":"real-semantic-router-plus-envoy",
            "cases":self.records, "passed":sum(not r["error"] for r in self.records),
            "failed":sum(bool(r["error"]) for r in self.records) + bool(unexpected_calls),
            "unexpected_backend_calls":unexpected_calls,
            "readiness_probes":[{**probe,"envoy_evidence":[e for e in native.get("envoy",[]) if e.get("request_id")==probe["request_id"]]} for probe in self.readiness],
            "config_sha256":hashlib.sha256((ROOT / "generated" / self.args.environment / self.args.example / "router.yaml").read_bytes()).hexdigest()}
        (self.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(f'Report {self.output}: {report["passed"]} passed, {report["failed"]} failed')
        return report["failed"]

    def execute(self):
        self.wait_gateway()
        # Kubernetes readiness precedes node dataplane endpoint propagation.
        # Health probes are not inference and do not invoke model routing.
        if self.args.overlay == "mock":
            for service in self.backend_services:
                deadline = time.monotonic() + 30
                while True:
                    try:
                        prefix = ["exec", "deployment/envoy", "--"] if self.args.runtime == "kubernetes" else ["exec", "-T", "envoy"]
                        health = (ROOT / "tools/container/http-health.sh").read_text()
                        self.run_command(*prefix, "timeout", "2", "bash", "-c", health, "--", service, "8000", "/health")
                        break
                    except subprocess.CalledProcessError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError(f"backend health did not become reachable from Envoy: {service}")
                        time.sleep(0.5)
        chat = json.loads((ROOT / "tests/requests/chat-text.json").read_text())
        speech = json.loads((ROOT / "tests/requests/speech.json").read_text())
        chat_models = [m for m in self.models if m["api_format"] == "openai"]
        chat_backends = [m["service"] for m in chat_models]
        speech_backends = [m["service"] for m in self.models if m["api_format"] == "speech"]
        self.call("unauthenticated", "/v1/chat/completions", chat, [401], token=False)
        self.call("chat-auto", "/v1/chat/completions", chat, [200] if chat_backends else [503], chat_backends, kind="chat")
        self.call("chat-sse", "/v1/chat/completions", {**chat, "stream":True}, [200] if chat_backends else [503], chat_backends, kind="sse")
        self.call("speech-auto", "/v1/audio/speech", speech, [200] if speech_backends else [503], speech_backends, kind="wav")
        self.call("speech-sse", "/v1/audio/speech", {**speech,"stream_format":"sse"}, [200] if speech_backends else [503], speech_backends, kind="sse")
        self.call("speech-wrong-content-type", "/v1/audio/speech", speech, [415], content_type="text/plain")
        self.call("speech-unknown-field", "/v1/audio/speech", {**speech,"unknown_semantic_option":1}, [400])
        if speech_backends:
            self.call("speech-backend-error", "/v1/audio/speech", {**speech,"input":"fixture:backend-error"}, [422], speech_backends)
        model = "local/text" if "vllm" in self.pools else ("cloud/chat" if "cloud" in self.pools else "local/speech")
        self.call("explicit-model-cannot-bypass", "/v1/chat/completions", {**chat,"model":model}, [400])
        for recipe, allowed in [("local-only", [m["service"] for m in chat_models if m["pool"] != "cloud"]),
                                ("cloud-only", [m["service"] for m in chat_models if m["pool"] == "cloud"])]:
            registered = (bool(set(self.pools) & {"vllm", "omni"}) if recipe == "local-only" else "cloud" in self.pools)
            self.call(recipe+"-chat-with-forged-headers", "/v1/chat/completions", {**chat,"model":recipe},
                      [200] if allowed else ([503] if registered else [400]), allowed, kind="chat", headers={
                          "X-Selected-Model":"cloud/chat" if recipe == "local-only" else "local/text",
                          "X-VSR-Skip-Processing":"true", "X-VSR-Looper-Request":"true"})
        vision = json.loads((ROOT / "tests/requests/chat-vision.json").read_text())
        vision_backends = [m["service"] for m in chat_models if "image_input" in m["capabilities"]]
        first = self.call("caller-workflow-vision", "/v1/chat/completions", vision,
                          [200] if vision_backends else [503], vision_backends, kind="chat")
        if vision_backends and self.records[-1]["status"] == 200:
            # The caller explicitly consumes prose and constructs a second
            # request. No server-side workflow or hidden Chat call is involved.
            prose = json.loads(first)["choices"][0]["message"]["content"]
            self.call("caller-workflow-speech", "/v1/audio/speech", {**speech,"input":prose},
                      [200] if speech_backends else [503], speech_backends, kind="wav")
        if chat_backends:
            for mode, prompt in [("cancel", "fixture:slow-stream"), ("interrupt", "fixture:interrupt-stream")]:
                self.call("chat-stream-"+mode, "/v1/chat/completions", {**chat,"stream":True,"messages":[{"role":"user","content":prompt}]},
                          [200], chat_backends, stream_mode=mode)
        if any(m["api_format"] == "audio_generate" for m in self.models):
            audio = json.loads((ROOT / "tests/requests/audio-generate.json").read_text())
            audio_models = [m for m in self.models if m["api_format"] == "audio_generate"]
            self.call("audio-generate", "/v1/audio/generate", audio, [200], [m["service"] for m in audio_models], kind="wav")
            self.call("audio-generate-unsupported-sse", "/v1/audio/generate", {**audio,"stream_format":"sse"}, [400])
            self.call("audio-generate-invalid-duration", "/v1/audio/generate", {**audio,"audio_length":0}, [400])
            self.call("audio-generate-wrong-content-type", "/v1/audio/generate", audio, [415], content_type="audio/wav")
            for scope, pools in [("local-only", {"omni","vllm"}), ("cloud-only", {"cloud"})]:
                allowed = [m["service"] for m in audio_models if m["pool"] in pools]
                self.call(scope+"-audio-generate", "/v1/audio/generate", {**audio,"model":scope},
                          [200] if allowed else ([503] if set(self.pools)&pools else [400]), allowed, kind="wav")
            audio_chat = json.loads((ROOT / "tests/requests/chat-audio.json").read_text())
            video_chat = json.loads((ROOT / "tests/requests/chat-video.json").read_text())
            mixed = {**audio_chat,"messages":[{"role":"user","content":audio_chat["messages"][0]["content"] + video_chat["messages"][0]["content"][1:] + vision["messages"][0]["content"][1:]}]}
            for task, payload, caps in [("audio", audio_chat, {"audio_input"}), ("video", video_chat, {"video_input"}),
                                        ("mixed", mixed, {"audio_input","video_input","image_input"})]:
                allowed = [m["service"] for m in chat_models if caps <= set(m["capabilities"])]
                self.call("chat-media-"+task, "/v1/chat/completions", payload, [200] if allowed else [503], allowed, kind="chat")
                if task == "mixed":
                    self.call("chat-media-mixed-sse", "/v1/chat/completions", {**payload,"stream":True}, [200] if allowed else [503], allowed, kind="sse")
            tools = [{"type":"function","function":{"name":"record_color","description":"Return color data","parameters":{"type":"object","properties":{"color":{"type":"string"}},"required":["color"]}}}]
            allowed = [m["service"] for m in chat_models if "tools" in m["capabilities"]]
            self.call("chat-tools", "/v1/chat/completions", {**chat,"tools":tools,"tool_choice":"required"}, [200] if allowed else [503], allowed, kind="tools")
            allowed = [m["service"] for m in chat_models if "structured_json" in m["capabilities"]]
            self.call("chat-structured-json", "/v1/chat/completions", {**chat,"response_format":{"type":"json_object"}}, [200] if allowed else [503], allowed, kind="structured")
        if any(m["api_format"] == "images" for m in self.models):
            image_request = json.loads((ROOT / "tests/requests/images.json").read_text())
            image_models = [m for m in self.models if m["api_format"] == "images"]
            image_backends = [m["service"] for m in image_models]
            for response_format, kind in [("b64_json", "image-b64"), ("url", "image-url"), ("file", "image-file")]:
                self.call("images-"+response_format, "/v1/images/generations", {**image_request,"response_format":response_format}, [200], image_backends, kind=kind)
            self.call("images-backend-error", "/v1/images/generations", {**image_request,"prompt":"fixture:backend-error"}, [422], image_backends)
            self.call("images-unknown-field", "/v1/images/generations", {**image_request,"unhandled_option":True}, [400])
            self.call("images-wrong-content-type", "/v1/images/generations", image_request, [415], content_type="image/png")
            self.call("images-invalid-count", "/v1/images/generations", {**image_request,"n":0}, [400])
            for recipe, pool_set in [("local-only", {"vllm", "omni"}), ("cloud-only", {"cloud"})]:
                allowed = [m["service"] for m in image_models if m["pool"] in pool_set]
                registered = bool(set(self.pools) & pool_set)
                self.call(recipe+"-images", "/v1/images/generations", {**image_request,"model":recipe},
                          [200] if allowed else ([503] if registered else [400]), allowed, kind="image-b64",
                          headers={"X-Selected-Model":"cloud/images", "X-VSR-Skip-Processing":"true"})
        if any(m["api_format"] in {"transcription","translation","image_edit"} for m in self.models):
            file = ("file","silence.wav","audio/wav",(ROOT/"tests/requests/assets/silence.wav").read_bytes())
            for task,path in [("transcription","/v1/audio/transcriptions"),("translation","/v1/audio/translations")]:
                models = [m for m in self.models if m["api_format"]==task]
                allowed = [m["service"] for m in models]
                fields = [("model","auto"),("language","en"),("max_completion_tokens","32")]
                for fmt in ["json","text","verbose_json","srt","vtt"]:
                    self.multipart_call(task+"-"+fmt,path,fields+[("response_format",fmt)],[file],[200] if allowed else [503],allowed,kind="asr-text" if fmt in {"text","srt","vtt"} else "asr-json")
                self.multipart_call(task+"-sse",path,fields+[("stream","true")],[file],[200] if allowed else [503],allowed,kind="sse")
                for scope,pools in [("local-only",{"vllm","omni"}),("cloud-only",{"cloud"})]:
                    selected = [m["service"] for m in models if m["pool"] in pools]
                    self.multipart_call(scope+"-"+task,path,[("model",scope),*fields[1:]],[file],[200] if selected else ([503] if set(self.pools)&pools else [400]),selected,kind="asr-json")
            models = [m for m in self.models if m["api_format"]=="transcription"]
            allowed = [m["service"] for m in models]
            fields = [("model","auto"),("max_completion_tokens","32")]
            if allowed:
                self.multipart_call("asr-backend-error","/v1/audio/transcriptions",fields+[("prompt","fixture:backend-error")],[file],[422],allowed)
            self.multipart_call("asr-wrong-content-type","/v1/audio/transcriptions",fields,[file],[415],content_type="application/json")
            self.multipart_call("asr-unknown-field","/v1/audio/transcriptions",fields+[("unhandled","x")],[file],[400])
            self.multipart_call("asr-duplicate-model","/v1/audio/transcriptions",fields+[("model","cloud-only")],[file],[400])
            self.multipart_call("asr-missing-file","/v1/audio/transcriptions",fields,[],[400])
            self.multipart_call("asr-explicit-model","/v1/audio/transcriptions",[("model",self.models[0]["name"]),*fields[1:]],[file],[400])
            image = base64.b64decode(vision["messages"][0]["content"][1]["image_url"]["url"].split(",",1)[1])
            image_file = ("image","blue.png","image/png",image)
            edit_models = [m for m in self.models if m["api_format"]=="image_edit"]
            allowed = [m["service"] for m in edit_models]
            edit = [("model","auto"),("prompt","Make this image brighter"),("response_format","b64_json"),("seed","7")]
            self.multipart_call("image-edit","/v1/images/edits",edit,[image_file],[200] if allowed else [503],allowed,kind="image-b64")
            self.multipart_call("image-edit-mask","/v1/images/edits",edit,[image_file,("mask_image","mask.png","image/png",image)],[200] if allowed else [503],allowed,kind="image-b64")
            self.multipart_call("image-edit-missing-input","/v1/images/edits",edit,[],[400])
        video_models = [m for m in self.models if m["api_format"]=="video_sync"]
        video_backends = [m["service"] for m in video_models]
        video_fields = [("model","auto"),("prompt","A blue square moves slowly"),("seconds","1"),("fps","5"),("num_frames","5"),("seed","7"),("size","64x64")]
        video_status = [200] if video_backends else [503]
        self.multipart_call("video-text", "/v1/videos/sync", video_fields, [], video_status, video_backends, kind="video-file")
        if video_models:
            image = base64.b64decode(vision["messages"][0]["content"][1]["image_url"]["url"].split(",",1)[1])
            files = [("input_reference","blue.png","image/png",image),
                     ("input_references","blue.mp4","video/mp4",(ROOT/"tests/requests/assets/blue.mp4").read_bytes()),
                     ("input_references","silence.wav","audio/wav",(ROOT/"tests/requests/assets/silence.wav").read_bytes())]
            self.multipart_call("video-mixed-files", "/v1/videos/sync", video_fields+[("generate_sound","true")], files, [200], video_backends, kind="video-file")
            refs = [("image_reference",json.dumps({"image_url":"https://example.invalid/blue.png"})),
                    ("video_reference",json.dumps({"video_url":"https://example.invalid/blue.mp4"})),
                    ("audio_reference",json.dumps({"audio_url":"https://example.invalid/silence.wav"}))]
            self.multipart_call("video-mixed-urls", "/v1/videos/sync", video_fields+refs, [], [200], video_backends, kind="video-file")
            self.multipart_call("video-backend-error", "/v1/videos/sync", [(k,"fixture:backend-error" if k=="prompt" else v) for k,v in video_fields], [], [422], video_backends)
        for scope,pools in [("local-only",{"vllm","omni"}),("cloud-only",{"cloud"})]:
            selected = [m["service"] for m in video_models if m["pool"] in pools]
            self.multipart_call(scope+"-video", "/v1/videos/sync", [("model",scope),*video_fields[1:]], [], [200] if selected else ([503] if set(self.pools)&pools else [400]), selected, kind="video-file")
        for name,fields in [("unknown",video_fields+[("unknown","x")]),("local-path",video_fields+[("frame_interpolation_model_path","/tmp/model")]),
                            ("duration",[(k,"0" if k=="seconds" else v) for k,v in video_fields]),
                            ("duplicate",video_fields+[("model","cloud-only")]),("missing-prompt",[p for p in video_fields if p[0]!="prompt"]),
                            ("file-id",video_fields+[("image_reference",'{"file_id":"private-file"}')])]:
            self.multipart_call("video-invalid-"+name, "/v1/videos/sync", fields, [], [400])
        self.multipart_call("video-explicit-model", "/v1/videos/sync", [("model",self.models[0]["name"]),*video_fields[1:]], [], [400])
        self.multipart_call("video-content-type", "/v1/videos/sync", video_fields, [], [415], content_type="application/json")
        mixed = json.loads((ROOT/"tests/requests/chat-mixed-output.json").read_text())
        output_models = [m for m in chat_models if {"audio_output","audio_input","image_input"} <= set(m["capabilities"])]
        allowed = [m["service"] for m in output_models]
        self.call("chat-mixed-output", "/v1/chat/completions", mixed, [200] if allowed else [503], allowed, kind="chat-audio")
        self.call("chat-mixed-output-sse", "/v1/chat/completions", {**mixed,"stream":True}, [200] if allowed else [503], allowed, kind="chat-audio-sse")
        self.call("chat-audio-only-output", "/v1/chat/completions", {**mixed,"modalities":["audio"]}, [200] if allowed else [503], allowed, kind="chat-audio")
        for scope,pools in [("local-only",{"vllm","omni"}),("cloud-only",{"cloud"})]:
            selected = [m["service"] for m in output_models if m["pool"] in pools]
            self.call(scope+"-mixed-output", "/v1/chat/completions", {**mixed,"model":scope}, [200] if selected else ([503] if set(self.pools)&pools else [400]), selected, kind="chat-audio")
        for name,values in [("modality",{"modalities":["video"]}),("duplicate",{"modalities":["audio","audio"]}),("format",{"audio":{"format":"invalid"}}),("field",{"audio":{"format":"wav","unhandled":True}})]:
            self.call("chat-output-invalid-"+name, "/v1/chat/completions", {**mixed,**values}, [400])
        if allowed:
            for mode,prompt in [("cancel","fixture:slow-stream"),("interrupt","fixture:interrupt-stream")]:
                request = {**mixed,"stream":True,"messages":[{"role":"user","content":prompt}]}
                self.call("chat-output-stream-"+mode, "/v1/chat/completions", request, [200], allowed, stream_mode=mode)
        if any(m["api_format"] == "mlx_video" for m in self.models):
            from mlx_video_cases import mock_cases
            mock_cases(self)
        self.video_tasks()
        if any(m["api_format"] in {"realtime","speech_stream"} for m in self.models):
            from websocket_cases import realtime_cases
            realtime_cases(self)
        return self.collect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--runtime", choices=["kubernetes","docker"], required=True)
    parser.add_argument("--example", required=True)
    parser.add_argument("--overlay", choices=["mock"], required=True)
    parser.add_argument("--url")
    parser.add_argument("--catalog", default="config/models/catalog.yaml")
    raise SystemExit(1 if Run(parser.parse_args()).execute() else 0)
