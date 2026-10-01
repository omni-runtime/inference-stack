"""Embedding acceptance through real Envoy + native SR, with declared mock engines."""
import base64
import hashlib
import json
import math
import struct


def mock_cases(run):
    models=[m for m in run.models if m['api_format']=='embeddings']
    def call(name, payload, statuses, selected=None, **kwargs):
        body=run.call('embedding-'+name,'/v1/embeddings',payload,statuses,[selected['service']] if selected else [],**kwargs)
        record=run.records[-1]
        record['embedding_parameters_sha256']=hashlib.sha256(json.dumps({k:v for k,v in payload.items() if k not in {'model','embedding_space'}},sort_keys=True).encode()).hexdigest()
        record['native_output']=True
        if selected and record['status']==200 and not record['error']:
            try:
                response=json.loads(body)
                assert response['model']==selected['provider_model_id']
                assert record['response_headers']['x-vsr-embedding-space']==selected['embedding']['space']
                inputs=payload.get('input',[payload.get('messages')])
                if isinstance(inputs,str) or inputs and isinstance(inputs[0],int):inputs=[inputs]
                assert len(response['data'])==len(inputs)
                dimensions=payload['dimensions']
                for index,item in enumerate(response['data']):
                    assert item['index']==index
                    values=item['embedding']
                    if payload.get('encoding_format')=='base64':
                        raw=base64.b64decode(values,validate=True)
                        assert len(raw)==4*dimensions
                        values=struct.unpack('<'+'f'*dimensions,raw)
                    assert len(values)==dimensions and all(math.isfinite(v) for v in values)
                    assert abs(sum(v*v for v in values)-1)<1e-5
                assert response['usage']['prompt_tokens']>0
            except (AssertionError,ValueError,KeyError,TypeError) as error:
                record['error']='embedding result contract: '+str(error)
        return body
    local=next(m for m in models if m['pool']!='cloud')
    cloud=next(m for m in models if m['pool']=='cloud')
    for model,scope in [(local,'local-only'),(cloud,'cloud-only')]:
        dimensions=model['embedding'].get('min_dimensions',model['embedding']['dimensions'])
        for encoding in ['float','base64']:
            call(scope+'-'+encoding,{'model':scope,'input':'represent a blue square','dimensions':dimensions,'encoding_format':encoding,'user':'embedding-fixture'},[200],model)
        call('auto-'+scope,{'model':'auto','embedding_space':model['embedding']['space'],'input':'hello','dimensions':dimensions},[200],model)
    dims=cloud['embedding'].get('min_dimensions',cloud['embedding']['dimensions'])
    call('batch',{'model':'cloud-only','input':['first','second'],'dimensions':dims},[200],cloud)
    if 'embedding_token_input' in cloud['capabilities']:
        call('token-batch',{'model':'cloud-only','input':[[1,2,3],[4]],'dimensions':dims},[200],cloud)
    local_request={'model':'local-only','input':'hello','dimensions':local['embedding'].get('min_dimensions',local['embedding']['dimensions'])}
    call('forged-headers',local_request,[200],local,headers={'X-Selected-Model':cloud['name'],'X-VSR-Skip-Processing':'true','X-VSR-Embedding-Space':cloud['embedding']['space']})
    call('unauthenticated',local_request,[401],token=False)
    call('ambiguous',{'model':'auto','input':'hello'},[400])
    call('space-conflict',{**local_request,'embedding_space':cloud['embedding']['space']},[503])
    call('bad-dimensions',{**local_request,'dimensions':1},[503])
    call('batch-limit',{**local_request,'input':['hello']*(local['embedding']['max_batch_size']+1)},[503])
    call('token-support',{**local_request,'input':[1,2,3]},[503])
    for name,change in [('empty',{'input':[]}),('mixed',{'input':['x',1]}),('stream',{'stream':True}),('unknown',{'unknown':True})]:
        call(name,{**local_request,**change},[400])
    call('content-type',local_request,[415],content_type='text/plain')
    call('backend-error',{**local_request,'input':'fixture:backend-error'},[422],local)
    if 'image_input' in local['capabilities']:
        messages=[{'role':'user','content':[{'type':'text','text':'blue square'},{'type':'image_url','image_url':{'url':'https://example.invalid/blue.png'}}]}]
        call('image',{'model':'local-only','messages':messages,'dimensions':local_request['dimensions']},[200],local)
        call('cloud-image-rejected',{'model':'cloud-only','messages':messages,'dimensions':dims},[503])
    run.call('embedding-chat-isolation','/v1/chat/completions',{'model':'auto','messages':[{'role':'user','content':'hello'}]},[503])


if __name__=='__main__':
    import argparse
    from pathlib import Path
    from run import Run
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,required=True)
    p.add_argument('--url')
    args=p.parse_args()
    for name in ('environment','runtime','example','overlay','catalog'):setattr(args,name,None)
    run=Run(args)
    if args.overlay!='mock':raise SystemExit('mock embedding acceptance requires mode: mock')
    run.wait_gateway()
    mock_cases(run)
    raise SystemExit(1 if run.collect() else 0)
