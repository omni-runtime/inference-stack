"""Native H3 transport cases; no model selection or inference emulation here."""
def mock_cases(run):
    endpoint = '/v1/video/generations'
    models = [m for m in run.models if m['api_format'] == 'mlx_video']
    allowed = [m['service'] for m in models]
    request = {'model':'auto','prompt':'A blue bird flying.','width':256,'height':256,
               'num_frames':22,'steps':4,'turbo':True,'seed':7,'fast':False,'chain_windows':1}
    run.call('mlx-video-json', endpoint, request, [200], allowed, kind='mlx-json')
    run.call('mlx-video-sse', endpoint, {**request,'stream':True}, [200], allowed, kind='mlx-sse')
    for scope,pools in [('local-only',{'omni','vllm'}),('cloud-only',{'cloud'})]:
        selected=[m['service'] for m in models if m['pool'] in pools]
        run.call(scope+'-mlx-video', endpoint, {**request,'model':scope},
                 [200] if selected else [503], selected, kind='mlx-json',
                 headers={'X-Selected-Model':'cloud/chat','X-VSR-Skip-Processing':'true'})
    run.call('mlx-video-backend-error',endpoint,{**request,'prompt':'fixture:backend-error'},[422],allowed)
    run.call('mlx-video-explicit-model',endpoint,{**request,'model':models[0]['name']},[400])
    run.call('mlx-video-content-type',endpoint,request,[415],content_type='text/plain')
    for name,change in [('unknown',{'unknown':1}),('dimension',{'width':255}),('turbo-steps',{'steps':3}),
                        ('local-path',{'lora_paths':['/tmp/model']}),('refs',{'ref_images':[]}),
                        ('chat-budget',{'max_tokens':32}),('keyframe-url',{'first_frame_image':'https://example.invalid/x.png'})]:
        run.call('mlx-video-invalid-'+name,endpoint,{**request,**change},[400])
