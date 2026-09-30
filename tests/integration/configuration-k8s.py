#!/usr/bin/env python3
"""Check real-model configurations without applying or replacing workloads."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def main():
    out = ROOT/'reports/kubernetes/kubernetes'/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-real-configuration')
    out.mkdir(parents=True)
    cases = []
    for example in sorted(p.name for p in (ROOT/'examples').iterdir()):
        command = [sys.executable, 'scripts/deploy.py', 'check', '--runtime', 'kubernetes',
                   '--environment', 'kubernetes', '--example', example]
        p = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        (out/(example+'.log')).write_text(p.stdout)
        blocked = example in {'local-vllm-omni','vllm-omni-cloud'}
        if blocked:
            status = 'blocked-capacity' if p.returncode==2 and 'lacks verified concurrent-engine capacity' in p.stdout else 'failed'
        else:
            status = 'configuration-validated' if p.returncode==0 else 'failed'
        cases.append({'example':example,'status':status,'exit_code':p.returncode,'command':command,'log':example+'.log'})
    report = {'runtime':'kubernetes','environment':'kubernetes','backend_type':'real',
              'method':'release/resource checks and native kubectl client dry-run; no apply, inference or model startup',
              'cases':cases,'validated':sum(c['status']=='configuration-validated' for c in cases),
              'blocked':sum(c['status']=='blocked-capacity' for c in cases),'failed':sum(c['status']=='failed' for c in cases)}
    (out/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(f'{out}: {report["validated"]} validated, {report["blocked"]} capacity-blocked, {report["failed"]} failed')
    return bool(report['failed'])


if __name__ == '__main__':
    raise SystemExit(main())
