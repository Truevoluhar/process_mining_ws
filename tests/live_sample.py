"""Optional live integration: GUID via stdin; never prints credentials or source contents."""
import json
from pathlib import Path
import sys
import time
import httpx


def main():
    guid=sys.stdin.readline().strip()
    if not guid:raise SystemExit('No valid local session available for live test.')
    with httpx.Client(base_url='http://host.docker.internal:8087',timeout=90,headers={'Authorization':'Bearer '+guid}) as client:
        for path in ('/health','/v1/schema','/openapi.json','/'):
            r=client.get(path);r.raise_for_status()
        with Path('/sample.csv').open('rb') as f:
            r=client.post('/v1/datasets',files={'files':('data_for_analysis.csv',f,'text/csv')})
        r.raise_for_status();dataset=r.json()
        r=client.post('/v1/analyses',json={'dataset_id':dataset['id'],'instructions':'Analiziraj celoten dnevnik. Prednost imajo anomalije v poti zadev, casovna neskladja in dolga cakanja. Numeričnim kodam ne pripisuj poslovnih imen brez šifranta.'})
        r.raise_for_status();job=r.json();print('Live job:',job['id'],flush=True)
        previous=None;deadline=time.monotonic()+1800
        while time.monotonic()<deadline:
            try:
                r=client.get('/v1/analyses/'+job['id']);r.raise_for_status();job=r.json()
            except httpx.HTTPError:
                print('Connection interrupted; monitoring continues.',flush=True);time.sleep(10);continue
            state=(job['status'],job['phase'])
            if state!=previous:print('State:',*state,flush=True);previous=state
            if job['status']=='completed':
                analysis=job['analysis']
                print(json.dumps({'metrics':analysis['metrics'],'sources':[{'rows_read':s['rows_read'],'rows_total':s['rows_total']} for s in analysis['sources']],
                                  'findings':len(analysis['findings']),'cases_shown':len(analysis['cases']),'nodes':len(analysis['nodes']),'edges':len(analysis['edges'])},ensure_ascii=False),flush=True)
                return
            if job['status']=='failed':raise SystemExit(job['error'])
            time.sleep(10)
        raise SystemExit('Live review still pending; persisted job remains available in GUI.')

if __name__=='__main__':main()
