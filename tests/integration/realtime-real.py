#!/usr/bin/env python3
"""Verify actual Omni speech WebSocket audio through the real gateway."""
import argparse
import hashlib
import io
import json
import struct
import time
from urllib.parse import urlencode
import uuid
import wave

from run import Run
from websocket_cases import WebSocket, websocket_case, headers_only_extproc


class RealRealtime(Run):
    def speech_session(self, pcm=False):
        path='/v1/audio/speech/stream?'+urlencode({'model':'local-only'})
        rid=str(uuid.uuid4()); ws=None; status=0; error=None; headers={}; metadata=[]; started=time.monotonic()
        allowed=[m['service'] for m in self.models if m['api_format']=='speech_stream' and m['pool']!='cloud']
        try:
            ws=WebSocket(self.url,path,self.token,rid,{'X-Selected-Model':'forged','X-VSR-Skip-Processing':'true'})
            status=ws.status;headers={k:v for k,v in ws.headers.items() if k.startswith('x-vsr-')}
            assert status==101,'native upgrade failed'
            ws.sock.settimeout(110)
            model=ws.headers['x-vsr-realtime-provider-model']
            assert model in {m['provider_model_id'] for m in self.models if m['api_format']=='speech_stream'},'unexpected native model'
            ws.send({'type':'session.config','model':model,'voice':'Vivian','task_type':'CustomVoice','language':'English',
                     'response_format':'pcm' if pcm else 'wav','stream_audio':pcm,'max_new_tokens':96})
            for utterance in range(2):
                begin=time.monotonic(); first_audio=None; chunks=[]; events=[]; total=0
                ws.send({'type':'input.text','text':'Hello. '});ws.send({'type':'input.text','text':'Have a good day.'});ws.send({'type':'input.done'})
                for _ in range(200):
                    opcode,payload=ws.receive()
                    if opcode==2:
                        if first_audio is None:first_audio=round((time.monotonic()-begin)*1000,2)
                        assert payload,'empty audio frame';chunks.append(payload);total+=len(payload);assert total<8*1024*1024
                    else:
                        assert opcode==1,'session closed before utterance finished'
                        event=json.loads(payload);events.append(event)
                        assert event['type']!='error','native engine reported '+event.get('message','error')
                        if event['type']=='session.done':break
                else:raise AssertionError('missing native completion')
                assert events[0]['type']=='audio.start' and events[0]['utterance_index']==utterance
                done=[e for e in events if e['type']=='audio.done'];assert len(done)==1 and not done[0]['error'] and done[0]['total_bytes']==total
                assert events[-1]['type']=='session.done' and chunks and total>100
                audio=b''.join(chunks)
                item={'utterance':utterance,'events':events,'binary_frames':len(chunks),'bytes':total,'sha256':hashlib.sha256(audio).hexdigest(),'first_audio_ms':first_audio}
                if pcm:
                    assert total%2==0 and events[0]['sample_rate']==24000
                    item.update(format='pcm16',sample_rate=24000)
                else:
                    with wave.open(io.BytesIO(audio)) as wav:
                        assert wav.getnframes()>0 and wav.getsampwidth()==2 and wav.getframerate()==24000
                        item.update(format='wav',frames=wav.getnframes(),channels=wav.getnchannels(),sample_rate=wav.getframerate())
                metadata.append(item)
            ws.send({'type':'session.close'});assert ws.receive()[0]==8
        except (OSError,ValueError,AssertionError,EOFError,KeyError,wave.Error) as exc:error=type(exc).__name__+': '+str(exc)
        finally:
            if ws:ws.sock.close()
        self.records.append({'case':'real-speech-websocket-'+('pcm-stream' if pcm else 'wav'),'request_id':rid,'client_path':path,
                             'status':status,'expected_statuses':[101],'expected_backend_set':allowed,'response_headers':headers,
                             'duration_ms':round((time.monotonic()-started)*1000,2),'error':error,'realtime_metadata':metadata,
                             'native_frames':{'sent':ws.sent,'received':ws.received} if ws else {}})

    def execute(self):
        self.wait_gateway()
        self.speech_session(False);self.speech_session(True)
        # The installed Qwen TTS model has no audio-input realtime capability.
        websocket_case(self,'real-audio-realtime-unsupported','/v1/realtime?model=local-only&sr_input=audio&sr_output=text',[503])
        failed=self.collect()
        # Explicit frame-processing assertion also applies to actual engines.
        report=json.loads((self.output/'report.json').read_text())
        for case in report['cases']:
            if case['status']==101:
                for event in case['envoy_upstream_evidence']:
                    if not headers_only_extproc(event):
                        case['error']='native WebSocket header-only ExtProc evidence missing or body calls present'
        report['passed']=sum(not c['error'] for c in report['cases']);report['failed']=sum(bool(c['error']) for c in report['cases'])+bool(report['unexpected_backend_calls'])
        (self.output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
        return failed or report['failed']


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--environment',required=True);p.add_argument('--runtime',choices=['kubernetes','docker'],required=True)
    p.add_argument('--example',required=True);p.add_argument('--catalog',default='config/models/catalog.yaml')
    p.add_argument('--overlay',choices=['real'],default='real');p.add_argument('--url')
    raise SystemExit(bool(RealRealtime(p.parse_args()).execute()))
