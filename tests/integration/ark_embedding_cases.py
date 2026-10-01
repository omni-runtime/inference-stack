"""Ark egress fixture acceptance through the real SR and Envoy."""
import argparse
import base64
import json
import math
import struct
from pathlib import Path
from run import Run


def cases(run):
    models={m['pool']:m for m in run.models}
    cloud=models['cloud']; local=next(m for m in run.models if m['pool']!='cloud')
    image={'type':'image_url','image_url':{'url':'https://example.invalid/test.png'}}
    def call(name,payload,statuses,model=None):
        result=run.call('ark-embedding-'+name,'/v1/embeddings',payload,statuses,[model['service']] if model else [])
        record=run.records[-1]
        if model and record['status']==200 and not record['error']:
            try:
                data=json.loads(result)
                assert data['model']==model['provider_model_id']
                assert record['response_headers']['x-vsr-embedding-space']==model['embedding']['space']
                assert len(data['data'])==1 and data['data'][0]['index']==0
                values=data['data'][0]['embedding'];dims=payload['dimensions']
                if payload.get('encoding_format')=='base64':values=struct.unpack('<'+'f'*dims,base64.b64decode(values,validate=True))
                assert len(values)==dims and all(math.isfinite(v) for v in values)
                assert abs(sum(v*v for v in values)-1)<1e-5
                assert data['usage']['prompt_tokens_details']=={'text_tokens':1,'image_tokens':1}
            except (AssertionError,ValueError,TypeError,KeyError,struct.error) as e:record['error']='Ark response contract: '+str(e)
        return record
    for dim in (1024,2048):
        for encoding in ('float','base64'):
            call(f'cloud-{dim}-{encoding}',{'model':'cloud-only','input':'blue sea','dimensions':dim,'encoding_format':encoding},[200],cloud)
    mixed=[{'role':'user','content':[{'type':'text','text':'blue sea'},image]}]
    call('cloud-image',{'model':'cloud-only','messages':mixed,'dimensions':1024},[200],cloud)
    call('local-image',{'model':'local-only','messages':mixed,'dimensions':64},[200],local)
    call('auto-cloud',{'model':'auto','embedding_space':cloud['embedding']['space'],'input':'sea','dimensions':1024},[200],cloud)
    call('ambiguous',{'model':'auto','input':'sea'},[400])
    call('dimension-gap',{'model':'cloud-only','input':'sea','dimensions':1536},[503])
    call('batch-rejected',{'model':'cloud-only','input':['a','b']},[503])
    call('tokens-rejected',{'model':'cloud-only','input':[1,2,3]},[503])
    call('instruction',{'model':'cloud-only','messages':[{'role':'system','content':'instruction'},{'role':'user','content':'sea'}],'dimensions':1024},[200],cloud)
    call('user-rejected',{'model':'cloud-only','input':'sea','user':'unsupported'},[400])
    record=call('backend-error',{'model':'cloud-only','input':'fixture:backend-error'},[429],cloud)
    assert 'x-vsr-embedding-space' not in record['response_headers']
    run.call('ark-chat-isolation','/v1/chat/completions',{'model':'cloud-only','messages':[{'role':'user','content':'hi'}]},[503])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--config',type=Path,required=True);parser.add_argument('--url');args=parser.parse_args()
    for name in ('environment','runtime','example','overlay','catalog'):setattr(args,name,None)
    run=Run(args)
    if args.overlay!='mock':raise SystemExit('Ark fixtures require mock mode')
    run.wait_gateway();cases(run);raise SystemExit(1 if run.collect() else 0)
