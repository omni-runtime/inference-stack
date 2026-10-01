#!/usr/bin/env python3
"""Explicit test model backend. It never makes routing decisions or calls models."""
import argparse
import base64
import hashlib
from email.parser import BytesParser
from email import policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import os
from pathlib import Path
import struct
import threading
import time
import wave
from urllib.parse import urlsplit, parse_qs
import zlib

EVIDENCE = []
JOBS = {}
NEXT_JOB_ID = 0
LOCK = threading.Lock()


def evidence(record):
    with LOCK:
        EVIDENCE.append(record)
        del EVIDENCE[:-10000]
        # Unbuffered stdout may write the JSON and newline separately. Keep both
        # writes under the same lock so concurrent stream callbacks stay JSONL.
        print(json.dumps(record), flush=True)


def audio_fixture():
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(24000)
        audio.writeframes(struct.pack("<h", 0) * 2400)
    return output.getvalue()


def image_fixture():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">2I5B", 2, 2, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress((b"\0" + b"\0\0\xff" * 2) * 2)) + chunk(b"IEND", b"")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def reply(self, code, body, content_type="application/json", extra=None):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Mock-Backend", self.server.backend_name)
        self.send_header("X-Request-ID", self.headers.get("X-Request-ID", ""))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body); self.wfile.flush()
            if self.headers.get("X-Request-ID"):
                evidence({"time":time.time(), "event":"response_sent", "backend_type":"mock", "backend":self.server.backend_name,
                          "request_id":self.headers["X-Request-ID"], "status":code, "response_sha256":hashlib.sha256(body).hexdigest()})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if urlsplit(self.path).path in {"/v1/realtime", "/v1/audio/speech/stream"}:
            self.websocket_session()
        elif self.path.startswith("/v1/videos/"):
            self.video_continuation("GET")
        elif self.path == "/health":
            self.reply(200, {"ready": True, "backend_type": "mock"})
        elif self.path == "/__evidence":
            with LOCK: records = list(EVIDENCE)
            self.reply(200, records)
        elif self.path == "/v1/models":
            self.reply(200, {"object": "list", "data": [{"id": self.server.backend_name, "object": "model"}]})
        else:
            self.reply(404, {"error": {"message": "mock endpoint not implemented"}})

    def do_DELETE(self):
        self.video_continuation("DELETE")

    def websocket_session(self):
        """Test-only native frame producer; it contains no routing logic."""
        rid = self.headers.get("X-Request-ID")
        authorized = self.headers.get("Authorization") == "Bearer " + os.environ.get(self.server.key_env, "")
        model = parse_qs(urlsplit(self.path).query).get("model", [""])[0]
        speech = urlsplit(self.path).path.endswith("/speech/stream")
        behavior = self.headers.get("X-Mock-Behavior", "normal")
        evidence({"time":time.time(), "event":"request_received", "backend_type":"mock", "backend":self.server.backend_name,
                  "path":self.path, "method":"GET", "request_id":rid, "authorization_matches":authorized, "provider_model":model})
        if not authorized or behavior == "reject":
            self.reply(401 if not authorized else 429, {"error":{"message":"native handshake rejected"}}); return
        key = self.headers.get("Sec-WebSocket-Key", "")
        accept = base64.b64encode(hashlib.sha1((key+"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket"); self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.send_header("X-Mock-Backend", self.server.backend_name)
        self.end_headers()
        self.connection.settimeout(15)
        self.close_connection = True
        sent, received, outcome = 0, 0, "peer_disconnected"

        def send(opcode, payload):
            nonlocal sent
            if isinstance(payload, dict): payload = json.dumps(payload, separators=(",", ":")).encode()
            length = len(payload)
            prefix = bytes([0x80 | opcode, length]) if length < 126 else bytes([0x80 | opcode, 126]) + struct.pack(">H", length)
            self.wfile.write(prefix + payload); self.wfile.flush()
            sent += 1
            evidence({"time":time.time(), "event":"websocket_sent", "backend":self.server.backend_name,
                      "request_id":rid, "index":sent, "opcode":opcode, "sha256":hashlib.sha256(payload).hexdigest(), "bytes":length})

        def exact(count):
            data = self.rfile.read(count)
            if len(data) != count: raise EOFError("peer closed")
            return data

        try:
            if not speech: send(1, {"type":"session.created", "id":"mock-session"})
            configured, text, audio, utterance, fragment = False, "", b"", 0, bytearray()
            while received < 100:
                first, second = exact(2)
                final, opcode, length = bool(first & 128), first & 15, second & 127
                if first & 112 or not second & 128: raise ValueError("invalid masked client frame")
                if length == 126: length = struct.unpack(">H", exact(2))[0]
                elif length == 127: length = struct.unpack(">Q", exact(8))[0]
                if length > 131072 or opcode >= 8 and (not final or length > 125): raise ValueError("oversized frame")
                mask = exact(4); payload = bytes(b ^ mask[i % 4] for i,b in enumerate(exact(length)))
                received += 1
                evidence({"time":time.time(), "event":"websocket_received", "backend":self.server.backend_name,
                          "request_id":rid, "index":received, "opcode":opcode, "sha256":hashlib.sha256(payload).hexdigest(), "bytes":length})
                if opcode == 8:
                    send(8,payload); outcome="client_close"; break
                if opcode == 9: send(10,payload); continue
                if opcode == 10: continue
                if opcode not in {0,1}: raise ValueError("expected native text frame")
                fragment.extend(payload)
                if len(fragment)>131072: raise ValueError("oversized message")
                if not final: continue
                try: message=json.loads(fragment)
                except ValueError:
                    send(1,{"type":"error","message":"invalid native JSON"}); fragment.clear(); continue
                fragment.clear()
                if behavior == "disconnect": outcome="backend_disconnected"; break
                kind=message.get("type")
                if kind in {"session.update", "session.config"}:
                    if message.get("model") != model:
                        send(1,{"type":"error","message":"native model does not match this engine"}); continue
                    configured=True
                    continue
                if not configured:
                    send(1,{"type":"error","message":"native session configuration required"}); continue
                if kind == "session.close": send(8,struct.pack(">H",1000)); outcome="session_close"; break
                if speech and kind == "input.text": text+=message.get("text",""); continue
                if not speech and kind == "input_audio_buffer.append":
                    audio+=base64.b64decode(message.get("audio",""),validate=True); continue
                if speech and kind == "input.done":
                    send(1,{"type":"audio.start","utterance_index":utterance,"sentence_index":0,"sentence_text":text,"format":"wav"})
                    send(2,audio_fixture())
                    send(1,{"type":"audio.done","utterance_index":utterance,"sentence_index":0})
                    send(1,{"type":"session.done","utterance_index":utterance,"total_sentences":1})
                    utterance+=1; text=""; continue
                if not speech and kind == "input_audio_buffer.commit":
                    chunks=30 if behavior=="slow" else 2
                    for i in range(chunks):
                        send(1,{"type":"transcription.delta","delta":f"word-{i} "}); time.sleep(0.08)
                    send(1,{"type":"transcription.done","text":"fixture transcription","usage":{"prompt_tokens":5,"completion_tokens":2}})
                    if self.server.backend_name != "vllm-asr":
                        send(1,{"type":"response.output_audio.delta","audio":base64.b64encode(b"\x01\x00"*160).decode(),"format":"pcm16","sample_rate_hz":16000})
                        send(1,{"type":"response.output_audio.done"})
                    audio=b""; continue
                send(1,{"type":"error","message":"unsupported native event"})
        except (BrokenPipeError, ConnectionResetError, EOFError):
            outcome="peer_disconnected"
        except (TimeoutError, ValueError) as error:
            outcome=type(error).__name__
            try: send(8,struct.pack(">H",1002))
            except (OSError, ValueError): pass
        finally:
            evidence({"time":time.time(), "event":"websocket_closed", "backend":self.server.backend_name,
                      "request_id":rid,"outcome":outcome,"sent":sent,"received":received})

    def video_continuation(self, method):
        authorized = self.headers.get("Authorization") == "Bearer " + os.environ.get(self.server.key_env, "")
        evidence({"time":time.time(), "event":"request_received", "backend_type":"mock", "backend":self.server.backend_name,
                  "path":self.path, "method":method, "request_id":self.headers.get("X-Request-ID"), "authorization_matches":authorized})
        if not authorized:
            self.reply(401, {"error":{"message":"mock credential mismatch"}}); return
        parts = self.path.split("/")
        job_id = parts[3] if len(parts)>3 else ""
        with LOCK:
            job = JOBS.get(job_id)
            if job and method == "DELETE":
                del JOBS[job_id]
            elif job:
                job["status"] = "completed"
                job["progress"] = 100
        if not job:
            self.reply(404, {"detail":"Video not found"})
        elif method == "DELETE":
            self.reply(200, {"id":job_id,"deleted":True})
        elif len(parts)==5 and parts[4]=="content":
            self.reply(200, Path("/config/blue.mp4").read_bytes(), "video/mp4", {"Content-Disposition":"attachment; filename=fixture.mp4"})
        else:
            self.reply(200, job)

    def stream_reply(self, chunks, content_type, *, interval=0.03, interrupt=False):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Mock-Backend", self.server.backend_name)
        self.send_header("X-Request-ID", self.headers.get("X-Request-ID", ""))
        self.end_headers()
        outcome, sent = "complete", 0
        try:
            for chunk in chunks:
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()
                sent += 1
                time.sleep(interval)
                if interrupt:
                    self.close_connection = True
                    outcome = "backend_interrupted"
                    break
            else:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            outcome = "client_cancelled"
            self.close_connection = True
        evidence({"time":time.time(), "event":"stream_finished", "backend_type":"mock",
                  "backend":self.server.backend_name, "request_id":self.headers.get("X-Request-ID"),
                  "outcome":outcome, "chunks_sent":sent, "response_sha256":hashlib.sha256(b"".join(chunks[:sent])).hexdigest()})

    def chat_audio_output(self, request, text):
        audio = {"id":"audio-mock", "expires_at":9999999999, "transcript":text, "data":base64.b64encode(audio_fixture()).decode()}
        metadata = {"sample_rate":24000, "num_channels":1, "format":"wav"}
        message = {"role":"assistant", "audio":audio}
        if "text" in request["modalities"]:
            message["content"] = text
        if not request.get("stream"):
            self.reply(200, {"id":"chatcmpl-audio", "object":"chat.completion", "created":1,"model":request["model"],
                "choices":[{"index":0,"message":message,"audio_metadata":metadata,"finish_reason":"stop"}],
                "usage":{"prompt_tokens":5,"completion_tokens":5,"total_tokens":10}})
            return
        prompt = json.dumps(request.get("messages", []))
        slow = "fixture:slow-stream" in prompt
        chunks = [{"id":"chatcmpl-audio", "object":"chat.completion.chunk", "created":1,"model":request["model"],
                   "choices":[{"index":0,"delta":message,"audio_metadata":metadata,"finish_reason":None}]}
                  for _ in range(30 if slow else 1)]
        chunks.append({"id":"chatcmpl-audio", "object":"chat.completion.chunk", "created":1,"model":request["model"],
                       "choices":[{"index":0,"delta":{},"finish_reason":"stop"}]})
        self.stream_reply([f'data: {json.dumps(c)}\n\n'.encode() for c in chunks]+[b'data: [DONE]\n\n'],
                          "text/event-stream", interval=0.1 if slow else 0.2, interrupt="fixture:interrupt-stream" in prompt)

    def do_POST(self):
        global NEXT_JOB_ID
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 16777216:
            self.reply(413, {"error": {"message": "invalid request size"}}); return
        body = self.rfile.read(length)
        files = []
        form_values = {}
        try:
            if self.headers.get("Content-Type", "").startswith("multipart/form-data"):
                message = BytesParser(policy=policy.default).parsebytes(("Content-Type: "+self.headers["Content-Type"]+"\r\nMIME-Version: 1.0\r\n\r\n").encode()+body)
                if not message.is_multipart():
                    raise ValueError("invalid multipart")
                for part in message.iter_parts():
                    name = part.get_param("name", header="content-disposition")
                    data = part.get_payload(decode=True)
                    if part.get_filename() is not None:
                        files.append({"name":name,"filename":part.get_filename(),"content_type":part.get_content_type(),"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest()})
                    else:
                        form_values.setdefault(name, []).append(data.decode())
                request = {name:values[0] for name,values in form_values.items()}
            else:
                request = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            self.reply(400, {"error": {"message": "invalid JSON"}}); return
        record = {"time": time.time(), "event":"request_received", "backend_type": "mock", "backend": self.server.backend_name,
                  "path": self.path, "model": request.get("model"), "request_id": self.headers.get("X-Request-ID"),
                  "body_sha256": hashlib.sha256(body).hexdigest(), "body_bytes": len(body),
                  "fields": sorted(request), "stream": request.get("stream", False)}
        if self.path in {"/v1/embeddings", "/api/v3/embeddings/multimodal", "/v1/embeddings/multimodal"}:
            record["embedding_parameters_sha256"] = hashlib.sha256(json.dumps({k:v for k,v in request.items() if k != "model"}, sort_keys=True).encode()).hexdigest()
        if self.path == "/v1/images/generations":
            record["image_parameters_sha256"] = hashlib.sha256(json.dumps({k:v for k,v in request.items() if k != "model"}, sort_keys=True).encode()).hexdigest()
        if self.path == "/v1/audio/generate":
            record["audio_parameters_sha256"] = hashlib.sha256(json.dumps({k:v for k,v in request.items() if k != "model"}, sort_keys=True).encode()).hexdigest()
        if self.path == "/v1/video/generations":
            record["video_parameters_sha256"] = hashlib.sha256(json.dumps({k:v for k,v in request.items() if k != "model"}, sort_keys=True).encode()).hexdigest()
        if "modalities" in request:
            record["chat_output_sha256"] = hashlib.sha256(json.dumps({k:request[k] for k in ("modalities","audio") if k in request},sort_keys=True).encode()).hexdigest()
        if "messages" in request:
            record["messages_sha256"] = hashlib.sha256(json.dumps(request["messages"], sort_keys=True).encode()).hexdigest()
        if form_values:
            record["form_sha256"] = hashlib.sha256(json.dumps({k:v for k,v in form_values.items() if k != "model"}, sort_keys=True).encode()).hexdigest()
            record["files"] = files
        credential = os.environ.get(self.server.key_env, "")
        record["authorization_matches"] = self.headers.get("Authorization") == "Bearer " + credential
        evidence(record)
        if not record["authorization_matches"]:
            self.reply(401, {"error":{"message":"mock provider credential mismatch"}}); return
        if request.get("input") == "fixture:backend-error":
            self.reply(422, {"error": {"message": "fixture rejected voice", "code": "voice_unavailable"}}); return
        if self.path in {"/v1/embeddings", "/api/v3/embeddings/multimodal", "/v1/embeddings/multimodal"}:
            ark = self.path.endswith('/embeddings/multimodal')
            inputs = request.get("input", [request.get("messages")])
            if ark:
                if inputs == [{"type":"text", "text":"fixture:backend-error"}]:
                    self.reply(429, {"error":{"message":"fixture rate limit","code":"RateLimit"}}); return
                inputs = [inputs]
            if isinstance(inputs, str) or inputs and isinstance(inputs[0], int):
                inputs = [inputs]
            dimensions = request.get("dimensions", 64)
            data = []
            for index, item in enumerate(inputs):
                digest = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).digest()
                vector = [(digest[i % len(digest)] - 127.5) / 127.5 for i in range(dimensions)]
                norm = math.sqrt(sum(value*value for value in vector))
                vector = [value / norm for value in vector]
                if request.get("encoding_format") == "base64":
                    vector = base64.b64encode(struct.pack("<"+"f"*dimensions, *vector)).decode()
                data.append({"object":"embedding", "index":index, "embedding":vector})
            if ark:
                data = dict(data[0]); data.pop('index')
            self.reply(200, {"object":"list", "data":data, "model":request["model"], "usage":{"prompt_tokens":len(inputs)*2,"total_tokens":len(inputs)*2,"prompt_tokens_details":{"text_tokens":1,"image_tokens":1}}}, extra={"X-VSR-Embedding-Space":"forged-upstream-space"})
        elif self.path in {"/v1/audio/transcriptions", "/v1/audio/translations"}:
            if request.get("prompt") == "fixture:backend-error":
                self.reply(422, {"error":{"message":"fixture audio rejection","code":"fixture_asr_error"}})
            elif request.get("stream") == "true":
                payload = [b'data: {"text":"mock transcript"}\n\n', b'data: [DONE]\n\n']
                self.stream_reply(payload, "text/event-stream")
            elif request.get("response_format") in {"text","srt","vtt"}:
                fmt = request["response_format"]
                text = {"text":"mock transcript", "srt":"1\n00:00:00,000 --> 00:00:00,100\nmock transcript\n", "vtt":"WEBVTT\n\n00:00.000 --> 00:00.100\nmock transcript\n"}[fmt]
                self.reply(200, text.encode(), "text/vtt" if fmt=="vtt" else "text/plain")
            else:
                self.reply(200, {"text":"mock transcript", "language":"en", "duration":0.1})
        elif self.path == "/v1/images/edits":
            self.reply(200, {"created":1,"data":[{"b64_json":base64.b64encode(image_fixture()).decode()}]})
        elif self.path == "/v1/videos":
            if request.get("prompt") == "fixture:invalid-task-response":
                self.reply(200, {"status":"queued"})
            elif request.get("prompt") == "fixture:backend-error":
                self.reply(422, {"error":{"message":"fixture video rejection","code":"fixture_video_error"}})
            else:
                with LOCK:
                    # Intentionally collide IDs across different instances to
                    # prove that public task identity includes the binding.
                    NEXT_JOB_ID += 1
                    job_id = "video_native_" + str(NEXT_JOB_ID)
                    job = {"id":job_id,"object":"video","status":"queued","progress":0,"model":request["model"]}
                    JOBS[job_id] = job
                evidence({"time":time.time(),"event":"video_created","backend_type":"mock","backend":self.server.backend_name,
                          "request_id":self.headers.get("X-Request-ID"),"provider_video_id":job_id})
                if request.get("prompt") == "fixture:slow-task-response":
                    time.sleep(8)
                self.reply(200, job)
        elif self.path == "/v1/video/generations":
            if request.get("prompt") == "fixture:backend-error":
                self.reply(422, {"error":{"message":"fixture MLX rejection","code":"fixture_video_error"}})
            else:
                result = {"frames":1,"height":32,"width":32,"fps":24,"format":"rgb8",
                          "data":base64.b64encode(bytes([0,0,255])*32*32).decode(),
                          "audio_sample_rate":48000,"audio_channels":2,"audio_format":"pcm_s16le",
                          "audio_data":base64.b64encode(bytes(8000)).decode(),"mock":True}
                if request.get("stream"):
                    events = [b'data: {"type":"progress","stage":"Generating","step":1,"total":4}\n\n',
                              ('data: '+json.dumps({"type":"complete",**result})+'\n\n').encode()]
                    self.stream_reply(events, "text/event-stream")
                else:
                    self.reply(200, result)
        elif self.path == "/v1/videos/sync":
            if request.get("prompt") == "fixture:backend-error":
                self.reply(422, {"error":{"message":"fixture video rejection","code":"fixture_video_error"}})
            else:
                self.reply(200, Path("/config/blue.mp4").read_bytes(), "video/mp4",
                           {"Content-Disposition":"attachment; filename=fixture.mp4"})
        elif self.path == "/v1/audio/speech":
            audio = audio_fixture()
            if request.get("stream") or request.get("stream_format"):
                if request.get("stream_format") == "audio":
                    self.stream_reply([audio[i:i+480] for i in range(0,len(audio),480)], "audio/wav")
                else:
                    events = [{"type":"speech.audio.delta", "audio":base64.b64encode(audio).decode()},
                              {"type":"speech.audio.done", "usage":{"input_tokens":2,"output_tokens":4}}]
                    self.stream_reply([f'data: {json.dumps(e)}\n\n'.encode() for e in events], "text/event-stream")
            else:
                self.reply(200, audio, "audio/wav")
        elif self.path == "/v1/audio/generate":
            self.reply(200, audio_fixture(), "audio/wav")
        elif self.path == "/v1/images/generations":
            if request.get("prompt") == "fixture:backend-error":
                self.reply(422, {"error":{"message":"fixture image rejection","type":"invalid_request_error","code":"fixture_image_error"}})
            elif request.get("response_format") == "file":
                self.reply(200, image_fixture(), "image/png", {"Content-Disposition":"attachment; filename=fixture.png"})
            else:
                item = ({"url":"https://example.invalid/mock-image.png"} if request.get("response_format") == "url"
                        else {"b64_json":base64.b64encode(image_fixture()).decode()})
                self.reply(200, {"created":1,"data":[item for _ in range(request.get("n", 1))],"mock":True})
        elif self.path == "/v1/chat/completions":
            text = f"mock response from {self.server.backend_name}"
            if "audio" in request.get("modalities", []):
                self.chat_audio_output(request, text)
                return
            if request.get("stream"):
                prompt = json.dumps(request.get("messages", []))
                slow = "fixture:slow-stream" in prompt
                chunks = [{"id":"chatcmpl-mock", "object":"chat.completion.chunk", "created":1,
                           "model":request["model"], "choices":[{"index":0,"delta":{"role":"assistant","content":text},"finish_reason":None}]}
                          for _ in range(30 if slow else 1)]
                chunks.append({"id":"chatcmpl-mock", "object":"chat.completion.chunk", "created":1,
                               "model":request["model"], "choices":[{"index":0,"delta":{},"finish_reason":"stop"}]})
                payload = [f'data: {json.dumps(c)}\n\n'.encode() for c in chunks] + [b"data: [DONE]\n\n"]
                self.stream_reply(payload, "text/event-stream", interval=0.1 if slow else 0.2,
                                  interrupt="fixture:interrupt-stream" in prompt)
            elif request.get("tools"):
                tool = request["tools"][0]["function"]["name"]
                self.reply(200, {"id":"chatcmpl-mock-tools","object":"chat.completion","created":1,"model":request["model"],
                    "choices":[{"index":0,"message":{"role":"assistant","content":None,"tool_calls":[{"id":"call_fixture","type":"function","function":{"name":tool,"arguments":'{"color":"blue"}'}}]},"finish_reason":"tool_calls"}],
                    "usage":{"prompt_tokens":5,"completion_tokens":5,"total_tokens":10}})
            else:
                if request.get("response_format", {}).get("type") in {"json_object", "json_schema"}:
                    text = '{"color":"blue"}'
                self.reply(200, {"id":"chatcmpl-mock", "object":"chat.completion", "created":1,
                    "model":request["model"], "choices":[{"index":0,"message":{"role":"assistant","content":text},"finish_reason":"stop"}],
                    "usage":{"prompt_tokens":5,"completion_tokens":5,"total_tokens":10}})
        else:
            self.reply(404, {"error":{"message":"mock endpoint not implemented"}})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--key-env", default="MODEL_API_KEY")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    server.backend_name = args.name
    server.key_env = args.key_env
    server.serve_forever()
