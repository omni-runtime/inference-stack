"""Bounded RFC 6455 test client and native protocol assertions (no server/proxy)."""
import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import time
from urllib.parse import urlsplit, urlencode
import uuid


def frame_record(opcode, payload):
    return {"opcode":opcode,"sha256":hashlib.sha256(payload).hexdigest(),"bytes":len(payload)}


def headers_only_extproc(event):
    # Envoy 1.37.3 omits body counters until a body call exists. Require the
    # actual typed filter state and both successful header calls before treating
    # absent body counters as zero; missing logging evidence is not a pass.
    info=event.get("extproc")
    return isinstance(info,dict) and info.get("request_header_call_status")==0 and info.get("response_header_call_status")==0 and all(info.get(k,0)==0 for k in ("request_body_call_count","response_body_call_count"))


class WebSocket:
    def __init__(self, gateway, path, token, request_id, extra=None):
        url = urlsplit(gateway)
        self.sock = socket.create_connection((url.hostname, url.port or (443 if url.scheme == "https" else 80)), timeout=15)
        if url.scheme == "https": self.sock = ssl.create_default_context().wrap_socket(self.sock,server_hostname=url.hostname)
        self.buffer = bytearray()
        self.sent, self.received = [], []
        self.key = base64.b64encode(os.urandom(16)).decode()
        headers = {"Host":url.netloc,"Upgrade":"websocket","Connection":"Upgrade",
                   "Sec-WebSocket-Version":"13","Sec-WebSocket-Key":self.key,"X-Request-ID":request_id}
        if token: headers["Authorization"]="Bearer "+token
        headers.update(extra or {})
        self.sock.sendall(("GET "+path+" HTTP/1.1\r\n"+"".join(k+": "+v+"\r\n" for k,v in headers.items())+"\r\n").encode())
        while b"\r\n\r\n" not in self.buffer:
            data=self.sock.recv(4096)
            if not data: raise EOFError("EOF during handshake")
            self.buffer.extend(data)
            if len(self.buffer)>65536: raise ValueError("oversized handshake")
        head, _, body=self.buffer.partition(b"\r\n\r\n"); self.buffer=bytearray(body)
        lines=head.decode("latin-1").split("\r\n"); self.status=int(lines[0].split()[1])
        self.headers={k.lower():v.strip() for k,v in (line.split(":",1) for line in lines[1:])}
        if self.status==101:
            expected=base64.b64encode(hashlib.sha1((self.key+"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            assert self.headers.get("sec-websocket-accept")==expected, "invalid native upgrade acceptance"
            assert self.headers.get("upgrade","").lower()=="websocket", "upgrade header lost"

    def send(self, payload, opcode=1, final=True):
        if isinstance(payload,dict): payload=json.dumps(payload,separators=(",",":")).encode()
        if isinstance(payload,str): payload=payload.encode()
        assert len(payload)<=131072
        mask=os.urandom(4); length=len(payload); first=(128 if final else 0)|opcode
        prefix=(bytes([first,128|length]) if length<126 else bytes([first,128|126])+struct.pack(">H",length) if length<65536 else bytes([first,128|127])+struct.pack(">Q",length))
        self.sock.sendall(prefix+mask+bytes(b^mask[i%4] for i,b in enumerate(payload)))
        self.sent.append(frame_record(opcode,payload))

    def exact(self,count):
        while len(self.buffer)<count:
            data=self.sock.recv(min(65536,count-len(self.buffer)))
            if not data: raise EOFError("native peer disconnected")
            self.buffer.extend(data)
        result=bytes(self.buffer[:count]);del self.buffer[:count];return result

    def receive(self):
        first,second=self.exact(2); opcode=first&15; length=second&127
        assert first&128 and not first&112 and not second&128, "invalid server frame"
        if length==126: length=struct.unpack(">H",self.exact(2))[0]
        elif length==127: length=struct.unpack(">Q",self.exact(8))[0]
        assert length<=2*1024*1024,"oversized server frame"
        payload=self.exact(length);self.received.append(frame_record(opcode,payload));return opcode,payload

    def event(self):
        opcode,data=self.receive();assert opcode==1,"expected text event";return json.loads(data)

    def close(self):
        if self.status==101:
            self.send(struct.pack(">H",1000),8)
            opcode,payload=self.receive();assert opcode==8 and payload[:2]==struct.pack(">H",1000),"close handshake changed"
        self.sock.close()


def websocket_case(run, name, path, statuses, backends=(), *, speech=False, behavior="normal", token=True, extra=None):
    rid=str(uuid.uuid4()); ws=None; error=None; status=0; response_headers={}; start=time.monotonic()
    record={"case":name,"request_id":rid,"client_path":path,"expected_statuses":statuses,
            "expected_backend_set":sorted(set(backends)),"status":0,"error":None}
    try:
        headers={"X-Mock-Behavior":behavior,"X-Selected-Model":"forged","X-VSR-Skip-Processing":"true"}
        headers.update(extra or {})
        ws=WebSocket(run.url,path,run.token if token else None,rid,headers)
        status=ws.status; response_headers={k:v for k,v in ws.headers.items() if k.startswith("x-vsr-") or k in {"x-mock-backend","upgrade","sec-websocket-accept"}}
        if status != 101:
            length=int(ws.headers.get("content-length",0))
            if 0 < length <= 65536:
                body=ws.exact(length)
                record["response_sha256"]=hashlib.sha256(body).hexdigest()
                try: record["error_code"]=json.loads(body).get("error",{}).get("code")
                except (ValueError,AttributeError): pass
        assert status in statuses,f"unexpected HTTP {status} ({record.get('error_code','unknown')})"
        if status==101:
            model=ws.headers.get("x-vsr-realtime-provider-model")
            assert model,"native frame model missing"
            record["provider_model"]=model
            if not speech: assert ws.event()["type"]=="session.created"
            ws.send(b"ping\x00",9); assert ws.receive()==(10,b"ping\x00"),"ping/pong changed"
            if behavior=="disconnect":
                ws.send({"type":"session.update","model":model})
                try: ws.receive()
                except (EOFError,ConnectionResetError): pass
                else: raise AssertionError("backend disconnect was hidden")
            else:
                config={"type":"session.config" if speech else "session.update","model":model}
                if speech: config.update({"voice":"Vivian","response_format":"wav","max_new_tokens":64})
                if behavior=="wrong-model":
                    ws.send({**config,"model":"unbound-model"});assert ws.event()["type"]=="error"
                # A fragmented configuration verifies frames remain transport data.
                raw=json.dumps(config).encode(); middle=len(raw)//2
                ws.send(raw[:middle],final=False);ws.send(raw[middle:],opcode=0)
                if speech:
                    for utterance in range(2):
                        ws.send({"type":"input.text","text":"Hello "});ws.send({"type":"input.text","text":"world."});ws.send({"type":"input.done"})
                        event=ws.event();assert event["type"]=="audio.start" and event["utterance_index"]==utterance
                        opcode,audio=ws.receive();assert opcode==2 and audio[:4]==b"RIFF" and audio[8:12]==b"WAVE","native audio file lost"
                        assert ws.event()["type"]=="audio.done";assert ws.event()["type"]=="session.done"
                    ws.send({"type":"session.close"});assert ws.receive()[0]==8
                else:
                    ws.send({"type":"input_audio_buffer.append","audio":base64.b64encode(b"\x00\x00"*1600).decode()})
                    ws.send({"type":"input_audio_buffer.commit","final":True})
                    assert ws.event()["type"]=="transcription.delta"
                    if behavior!="slow":
                        assert ws.event()["type"]=="transcription.delta"
                        done=ws.event();assert done["type"]=="transcription.done" and done["usage"]["completion_tokens"]>0
                        if ws.headers.get("x-mock-backend")!="vllm-asr":
                            event=ws.event();assert event["type"]=="response.output_audio.delta" and len(base64.b64decode(event["audio"],validate=True))>0
                            assert ws.event()["type"]=="response.output_audio.done"
                        ws.close()
                if behavior=="slow": ws.sock.shutdown(socket.SHUT_RDWR)
            record["websocket_frames"]={"sent":ws.sent,"received":ws.received,"behavior":behavior,"speech":speech}
        else:
            ws.close()
    except (OSError,ValueError,AssertionError,EOFError,KeyError) as exc:
        error=type(exc).__name__+": "+str(exc)
    finally:
        if ws: ws.sock.close()
    record.update({"status":status,"response_headers":response_headers,"error":error,"duration_ms":round((time.monotonic()-start)*1000,2)})
    run.records.append(record)


def realtime_cases(run):
    candidates=[m for m in run.models if m["api_format"] in {"realtime","speech_stream"}]
    for fmt,speech in [("realtime",False),("speech_stream",True)]:
        for scope,pools in [("auto",set(run.pools)),("local-only",{"vllm","omni"}),("cloud-only",{"cloud"})]:
            query={"model":scope}
            if not speech: query.update(sr_input="audio",sr_output="text")
            path=("/v1/audio/speech/stream" if speech else "/v1/realtime")+"?"+urlencode(query)
            allowed=[m["service"] for m in candidates if m["api_format"]==fmt and m["pool"] in pools]
            websocket_case(run,fmt+"-"+scope,path,[101] if allowed else [503] if set(run.pools)&pools else [400],allowed,speech=speech)
        if not speech:
            path="/v1/realtime?model=auto&sr_input=audio&sr_output=text%2Caudio"
            allowed=[m["service"] for m in candidates if m["api_format"]==fmt and "audio_output" in m["capabilities"]]
            websocket_case(run,"realtime-audio-output",path,[101] if allowed else [503],allowed)
    path="/v1/realtime?model=auto&sr_input=audio&sr_output=text"
    allowed=[m["service"] for m in candidates if m["api_format"]=="realtime"]
    if allowed:
        for behavior in ["wrong-model","reject","disconnect","slow"]:
            websocket_case(run,"realtime-"+behavior,path,[429] if behavior=="reject" else [101],allowed,behavior=behavior)
    websocket_case(run,"realtime-no-auth",path,[401],token=False)
    websocket_case(run,"realtime-bad-upgrade",path,[400],extra={"Sec-WebSocket-Key":"invalid"})
    websocket_case(run,"realtime-undeclared-input","/v1/realtime?model=auto",[400])
    websocket_case(run,"realtime-duplicate-model",path+"&model=cloud-only",[400])
    websocket_case(run,"realtime-concrete-bypass",path.replace("model=auto","model="+candidates[0]["name"] if candidates else "model=forged"),[400])
