"""Real cloud/local embeddings with SR and Envoy upstream evidence."""
import argparse
import base64
import json
import math
import struct
from pathlib import Path
from run import Run


def cases(run):
    local=next(m for m in run.models if m['name']=='local/embedding')
    cloud=next(m for m in run.models if m['name']=='cloud/embedding')
    def call(name, payload, expected=(200,), selected=None):
        raw=run.call('real-embedding-'+name,'/v1/embeddings',payload,list(expected),[selected['service']] if selected else [])
        record=run.records[-1]
        if record['status']==200 and selected and not record['error']:
            try:
                data=json.loads(raw);assert data['model']==selected['provider_model_id']
                assert record['response_headers']['x-vsr-embedding-space']==selected['embedding']['space']
                assert len(data['data'])==1 and data['data'][0]['index']==0
                values=data['data'][0]['embedding'];dimensions=payload.get('dimensions',selected['embedding']['dimensions'])
                if payload.get('encoding_format')=='base64':values=struct.unpack('<'+'f'*dimensions,base64.b64decode(values,validate=True))
                assert len(values)==dimensions and all(math.isfinite(v) for v in values)
                norm=sum(v*v for v in values);assert 0.95 < norm < 1.05
                assert data['usage']['prompt_tokens']>0 and data['usage']['total_tokens']>0
                record['embedding_dimensions']=len(values);record['norm_squared']=norm
                if selected['api_format']=='ark_embeddings':assert 'prompt_tokens_details' in data['usage']
            except (AssertionError,KeyError,ValueError,TypeError,struct.error) as error:
                record['error']='embedding contract: '+str(error)
        return raw
    for scope,model in [('local-only',local),('cloud-only',cloud)]:
        call(scope+'-text',{'model':scope,'input':'天很蓝，海很深'},selected=model)
        call(scope+'-base64',{'model':scope,'input':'蓝色海洋','dimensions':1024,'encoding_format':'base64'},selected=model)
        image=[{'role':'user','content':[{'type':'text','text':'蓝色的大海和天空'},{'type':'image_url','image_url':{'url':'https://ark-project.tos-cn-beijing.volces.com/images/view.jpeg'}}]}]
        call(scope+'-mixed',{'model':scope,'messages':image,'dimensions':1024},selected=model)
        call('auto-'+scope,{'model':'auto','embedding_space':model['embedding']['space'],'input':'hello','dimensions':1024},selected=model)
    call('cloud-instruction',{'model':'cloud-only','messages':[{'role':'system','content':'Target_modality: text and image.\nInstruction:Represent the mixed content for semantic retrieval\nQuery:'},{'role':'user','content':'蓝色大海'}],'dimensions':1024},selected=cloud)
    call('ambiguous',{'model':'auto','input':'hello'},(400,))
    call('dimension-gap',{'model':'cloud-only','input':'hello','dimensions':1536},(503,))
    call('batch-limit',{'model':'cloud-only','input':['first','second']},(503,))
    call('space-conflict',{'model':'local-only','input':'hello','embedding_space':cloud['embedding']['space']},(503,))
    call('token-support',{'model':'local-only','input':[1,2,3]},(503,))
    run.call('real-embedding-unauthorized','/v1/embeddings',{'model':'local-only','input':'hello'},[401],token=False)
    for scope,pool in [('local-only','vllm'),('cloud-only','cloud')]:
        model=next(m for m in run.models if m['pool']==pool and m['api_format']=='openai')
        run.call('embedding-regression-'+scope,'/v1/chat/completions',{'model':scope,'messages':[{'role':'user','content':'Say hello briefly.'}],'max_tokens':64},[200],[model['service']],kind='chat')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--config',type=Path,required=True);parser.add_argument('--url');args=parser.parse_args()
    for name in ('environment','runtime','example','overlay','catalog'):setattr(args,name,None)
    run=Run(args)
    if args.overlay!='real':raise SystemExit('real embedding acceptance requires mode: real')
    run.wait_gateway();cases(run);raise SystemExit(1 if run.collect() else 0)
