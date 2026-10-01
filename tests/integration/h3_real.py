#!/usr/bin/env python3
"""Generate a real short H3 clip through native SR/Envoy and retain media evidence."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
from run import Run, ROOT, StackConfig


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path)
    parser.add_argument('--environment')
    parser.add_argument('--runtime',choices=['kubernetes'])
    parser.add_argument('--example')
    parser.add_argument('--catalog')
    parser.add_argument('--url')
    args=parser.parse_args()
    args.overlay=None
    if not args.config:
        args.environment=args.environment or 'hybrid'
        args.runtime=args.runtime or 'kubernetes'
        args.example=args.example or 'vllm-omni-cloud'
        args.catalog=args.catalog or 'environments/hybrid/catalog.yaml'
        args.overlay='real'
    elif StackConfig(args.config).mock:
        parser.error('real H3 test requires mode: real')
    run=Run(args);run.wait_gateway()
    path='/v1/video/generations'
    request={'model':'local-only','prompt':'A small red ball rolls across a wooden table. Soft rolling sound. Static camera.',
             'width':256,'height':256,'num_frames':22,'steps':4,'turbo':True,'seed':7,'stream':True}
    run.call('h3-cloud-scope-rejected',path,{**request,'model':'cloud-only'},[503])
    run.call('h3-explicit-model-rejected',path,{**request,'model':'local/video'},[400])
    data=run.call('h3-real-short-video',path,request,[200],['mlx-h3'],kind='mlx-sse',timeout=3600)
    record=run.records[-1]
    if not record['error']:
        events=[json.loads(line[6:]) for line in data.splitlines() if line.startswith(b'data: ')]
        result=events[-1]
        if result.get('mock'):
            record['error']='real H3 response marked mock'
        else:
            rgb=base64.b64decode(result['data'],validate=True)
            pcm=base64.b64decode(result['audio_data'],validate=True)
            media=ROOT/'.state'/'media'/run.output.name
            media.mkdir(parents=True,exist_ok=True)
            (media/'frames.rgb').write_bytes(rgb);(media/'audio.pcm').write_bytes(pcm)
            metadata={k:result[k] for k in ['frames','height','width','fps','format','audio_sample_rate','audio_channels','audio_format']}
            metadata.update(rgb_sha256=hashlib.sha256(rgb).hexdigest(),pcm_sha256=hashlib.sha256(pcm).hexdigest(),
                            rgb_bytes=len(rgb),pcm_bytes=len(pcm),progress_events=len(events)-1,
                            video_duration=result['frames']/result['fps'],
                            audio_duration=len(pcm)/(2*result['audio_channels']*result['audio_sample_rate']))
            (media/'metadata.json').write_text(json.dumps(metadata,indent=2)+'\n')
            (run.output/'media.json').write_text(json.dumps(metadata,indent=2)+'\n')
            record['media']=metadata
            print('Media saved:',media,flush=True)
    raise SystemExit(1 if run.collect() else 0)


if __name__=='__main__':main()
