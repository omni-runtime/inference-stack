#!/usr/bin/env python3
"""Explicit operator lifecycle acceptance in the new project's Kubernetes namespace."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[2]
LABEL = 'app.kubernetes.io/part-of=inference-stack'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment', default='kubernetes')
    parser.add_argument('--catalog', default='tests/overlays/mock/catalog-media.yaml')
    args = parser.parse_args()
    env = yaml.safe_load((ROOT/'environments'/args.environment/'environment.yaml').read_text())
    if env['runtime'] != 'kubernetes':
        raise ValueError('this suite exercises Kubernetes; Docker has a separate runtime acceptance')
    k = env['kubernetes']
    cli = [sys.executable, str(ROOT/'scripts/deploy.py')]
    target = ['--runtime','kubernetes','--environment',args.environment]
    kubectl = ['kubectl','--kubeconfig',k['kubeconfig'],'--context',k['context'],'-n',k['namespace']]
    output = ROOT/'reports'/args.environment/'kubernetes'/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-lifecycle')
    output.mkdir(parents=True)
    report = {'environment':args.environment,'runtime':'kubernetes','backend_type':'mock',
              'namespace': k['namespace'], 'gateway_node_port': k['gateway_node_port'], 'cases':[]}
    state_file = ROOT/'.state'/args.environment/'kubernetes/inventory.json'
    assert 'vllm' not in json.loads(state_file.read_text())['stopped'], 'suite will not override a preexisting explicit stop'
    log = (output/'operations.log').open('w')

    def command(parts, capture=False, timeout=1000):
        log.write('$ '+ ' '.join(map(str,parts))+'\n');log.flush()
        result = subprocess.run(parts,cwd=ROOT,text=True,stdout=subprocess.PIPE if capture else log,stderr=log,timeout=timeout,check=True)
        return result.stdout if capture else None

    def kub(*parts):
        return command(kubectl+list(parts),capture=True)

    def deploy(example=None):
        extra = ['--example',example,'--overlay','mock','--catalog',args.catalog] if example else []
        command(cli+['deploy']+target+extra)

    def checkpoint(name, **details):
        report['cases'].append({'case':name,'status':'passed',**details})
        (output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
        print(name+': passed',flush=True)

    def data_identity():
        pvc = json.loads(kub('get','pvc','-l',LABEL,'-o','json'))
        return {'pvcs':{i['metadata']['name']:i['metadata']['uid'] for i in pvc['items']},
                'credential_uid':kub('get','secret','inference-credentials','-o','jsonpath={.metadata.uid}')}

    def replicas(service):
        d=json.loads(kub('get','deployment',service,'-o','json'))
        return d['spec'].get('replicas',1),d['status'].get('readyReplicas',0)

    def resources():
        d=json.loads(kub('get','deployment,service','-l',LABEL,'-o','json'))
        return sorted((i['kind'],i['metadata']['name']) for i in d['items'])

    restored = False
    try:
        for _ in range(2):
            command(['bash','scripts/install.sh',*target,'--example','vllm-omni-cloud','--overlay','mock','--catalog',args.catalog])
        checkpoint('repeated-install-reuses-dependencies')
        deploy('vllm-omni-cloud');before=resources();deploy('vllm-omni-cloud')
        assert resources()==before
        checkpoint('repeated-deploy-has-no-extra-services',resources=before)
        # Use the single local model scenario so no other healthy local Chat
        # backend can legitimately answer the stopped-model request.
        deploy('local-vllm')
        command(cli+['stop',*target,'--service','vllm'])
        assert replicas('vllm')==(0,0)
        deploy()
        assert replicas('vllm')==(0,0)
        checkpoint('explicit-stop-survives-redeploy')
        token=(ROOT/'secrets'/args.environment/'GATEWAY_TOKEN').read_text().strip()
        rid=str(uuid.uuid4())
        request=json.dumps({'model':'local-only','messages':[{'role':'user','content':'Lifecycle stop probe'}],'max_tokens':32}).encode()
        url=f'http://{k["ssh"].split("@")[-1]}:{k["gateway_node_port"]}'
        try:
            response=urllib.request.urlopen(urllib.request.Request(url+'/v1/chat/completions',request,{'Authorization':'Bearer '+token,'Content-Type':'application/json','X-Request-ID':rid}),timeout=20)
        except urllib.error.HTTPError as e:
            response=e
        with response:
            body=response.read(1048576);status=response.status
        assert replicas('vllm')==(0,0)
        native=[];deadline=time.monotonic()+10
        while not native and time.monotonic()<deadline:
            for line in kub('logs','deployment/envoy','--tail=200').splitlines():
                try:event=json.loads(line)
                except ValueError:continue
                if isinstance(event,dict) and event.get('request_id')==rid:native.append(event)
            if not native:time.sleep(0.1)
        assert len(native)==1 and native[0].get('selected_model')
        report['stop_probe']={'request_id':rid,'response_status':status,'native_envoy_evidence':native}
        (output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
        assert status in (502,503,504), status
        checkpoint('request-does-not-restart-explicitly-stopped-service',request_id=rid,response_status=status,
                   response_sha256=hashlib.sha256(body).hexdigest(),native_envoy_evidence=native)
        command(cli+['start',*target,'--service','vllm']);assert replicas('vllm')==(1,1)
        checkpoint('explicit-start-restores-service')
        deploy('vllm-omni-cloud')
        images=yaml.safe_load((ROOT/'versions.lock.yml').read_text())['images']
        kub('set','image','deployment/envoy','envoy='+images['python'])
        deadline=time.monotonic()+45
        failed_pods=[]
        while time.monotonic()<deadline:
            pods=json.loads(kub('get','pods','-l','app=envoy','-o','json'))
            failed_pods=[p['metadata']['name'] for p in pods['items'] if any(c.get('state',{}).get('waiting',{}).get('reason')=='CrashLoopBackOff' or c.get('state',{}).get('terminated',{}).get('exitCode',0)!=0 for c in p.get('status',{}).get('containerStatuses',[]))]
            if failed_pods:break
            time.sleep(0.5)
        assert failed_pods,'injected wrong runtime image did not produce an observed failure'
        deploy();assert replicas('envoy')==(1,1)
        checkpoint('explicit-redeploy-recovers-failed-rollout',failed_pods=failed_pods)
        identities=data_identity()
        command(cli+['down',*target])
        assert resources()==[]
        assert data_identity()==identities
        checkpoint('down-retains-storage-and-credentials',retained=identities)
        deploy();assert data_identity()==identities
        checkpoint('deploy-after-down-reuses-storage-and-credentials')
        deploy('cloud-only')
        actual={name for kind,name in resources() if kind=='Deployment'}
        catalog=yaml.safe_load((ROOT/args.catalog).read_text())
        expected={'router','envoy'} | {m['service'] for m in catalog['models'] if m['pool']=='cloud'}
        if any(m['pool']=='cloud' and m['api_format']=='video' for m in catalog['models']):
            expected.add('media-bindings')
        assert actual==expected,(actual,expected)
        checkpoint('example-switch-removes-obsolete-owned-services',deployments=sorted(actual))
        deploy('vllm-omni-cloud');restored=True
        checkpoint('restore-full-mock-example')
    except Exception as error:
        report['cases'].append({'case':'lifecycle-error','status':'failed','error':str(error)})
    finally:
        if not restored:
            try:
                state=json.loads(state_file.read_text())
                if 'vllm' in state['stopped']:
                    command(cli+['start',*target,'--service','vllm'])
                deploy('vllm-omni-cloud')
                report['recovery']='restored-full-mock-example'
            except Exception as error:
                report['recovery']='failed: '+str(error)
        report['passed']=sum(c['status']=='passed' for c in report['cases'])
        report['failed']=sum(c['status']=='failed' for c in report['cases'])
        (output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
        log.close()
    print(output,flush=True)
    return int(report['failed']!=0)


if __name__=='__main__':
    raise SystemExit(main())
