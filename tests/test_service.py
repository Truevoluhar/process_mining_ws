import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
import httpx
from fastapi.testclient import TestClient
import app
from report import validate, schema


def result(source):
    return dict(summary='Agentov povzetek',coverage='Celoten naložen dnevnik; prikaz brez poti.',
                sources=[dict(source_id=source,role='dnevnik',rows_read=2,rows_total=2,coverage='vse vrstice')],
                metrics=[dict(label='Zadeve',value='1',explanation='Izračunal agent')],nodes=[],edges=[],variants=[],cases=[],findings=[],recommendations=[],limitations=[])


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();app.DATA=Path(self.tmp.name);app.DB=app.DATA/'test.sqlite3'
        self.calls=[];self.lost=False;self.pending=False;self.bad=False;self.offline=False
        self.client=TestClient(app.app);self.client.__enter__()
        self.client.portal.call(app.app.state.http.aclose)
        app.app.state.http=httpx.AsyncClient(transport=httpx.MockTransport(self.remote))
        self.headers={'Authorization':'Bearer owner'}

    def tearDown(self):
        self.client.__exit__(None,None,None);self.tmp.cleanup()

    def remote(self,r):
        self.calls.append(r);p=r.url.path
        if p=='/v1/auth/me':return httpx.Response(200,json={'user_id':r.url.params['guid'],'username':'test'})
        if p=='/v1/files':return httpx.Response(200,json={'path':'/uploads/raw.csv'})
        if p=='/v1/threads':return httpx.Response(200,json={'thread_id':'thread'})
        if p.endswith('/runs'):
            self.body=json.loads(r.content)
            if self.lost:raise httpx.ReadTimeout('accepted; response lost',request=r)
            return httpx.Response(202,json={'run_id':'run'})
        if p=='/v1/runs/active':return httpx.Response(200,json={'thread_id':'thread','run_id':'run','status':'running'})
        if p=='/v1/runs/run':
            if self.offline:raise httpx.ConnectError('offline',request=r)
            return httpx.Response(200,json={'status':'running' if self.pending else 'completed','phase':'Agent izvaja opravilo'})
        if p.endswith('/messages'):
            path='/exports/process_mining/'+self.job+'/report.json'
            return httpx.Response(200,json=[{'role':'assistant','content':json.dumps({'report_path':path})}])
        if p.startswith('/v1/files/exports/'):
            data=result(self.source if not self.bad else 'invented-source')
            return httpx.Response(200,json=data)
        return httpx.Response(404)

    def create(self,files=None):
        files=files or [('files',('events.csv',b'CASE_ID;ACTIVITY\n1;A\n1;B\n','text/csv'))]
        r=self.client.post('/v1/datasets',headers=self.headers,files=files)
        self.assertEqual(r.status_code,201,r.text);d=r.json();self.source=d['files'][0]['id']
        # Hold the run until the test has captured its public ID.
        self.pending=True
        r=self.client.post('/v1/analyses',headers=self.headers,json={'dataset_id':d['id'],'instructions':'Preveri cakanje.'})
        self.assertEqual(r.status_code,202,r.text);self.job=r.json()['id'];return d

    def wait(self):
        self.pending=False
        deadline=time.monotonic()+12
        while time.monotonic()<deadline:
            r=self.client.get('/v1/analyses/'+self.job,headers=self.headers)
            if r.json()['status'] not in ('queued','running'):return r.json()
            time.sleep(.05)
        self.fail('Job did not finish')

    def test_only_agent_analyzes_original_bytes_and_json_is_owned(self):
        self.create();job=self.wait();self.assertEqual(job['status'],'completed',job)
        self.assertEqual(job['analysis']['metrics'][0]['value'],'1')
        self.assertEqual(self.body['attachments'],[])
        uploads=[r for r in self.calls if r.method=='POST' and r.url.path=='/v1/files']
        self.assertEqual(len(uploads),1);self.assertIn(b'CASE_ID;ACTIVITY\n1;A\n1;B\n',uploads[0].content)
        self.assertNotIn('response_format',self.body)
        self.assertEqual(list(self.body['response_schema']['properties']),['report_path'])
        self.assertIn('Preveri cakanje.',self.body['message'])
        self.assertNotIn('guid',job);self.assertNotIn('upload_files',job)
        r=self.client.get('/v1/analyses/'+self.job,headers={'Authorization':'Bearer other'})
        self.assertEqual(r.status_code,404)

    def test_lost_start_response_recovers_without_duplicate_run(self):
        self.lost=True;self.create();self.assertEqual(self.wait()['status'],'completed')
        self.assertEqual(sum(r.method=='POST' and r.url.path.endswith('/runs') for r in self.calls),1)

    def test_running_remains_running_during_transient_disconnect_and_single_active(self):
        self.create();self.offline=True;time.sleep(.1)
        r=self.client.get('/v1/analyses/'+self.job,headers=self.headers);self.assertEqual(r.json()['status'],'running')
        d=r.json()['dataset_id']
        r=self.client.post('/v1/analyses',headers=self.headers,json={'dataset_id':d})
        self.assertEqual(r.status_code,409)
        self.offline=False;self.assertEqual(self.wait()['status'],'completed')

    def test_invalid_agent_provenance_is_not_published(self):
        self.bad=True;self.create();job=self.wait()
        self.assertEqual(job['status'],'failed');self.assertIsNone(job['analysis'])

    def test_other_files_not_locally_parsed(self):
        self.create([('files',('unknown.bin',b'opaque not CSV or executable','application/octet-stream'))])
        self.assertEqual(self.wait()['status'],'completed')
        self.assertIn(b'opaque not CSV or executable',next(r.content for r in self.calls if r.url.path=='/v1/files'))

    def test_dataset_ownership_and_empty_upload(self):
        self.create()
        with app.db() as conn:row=conn.execute('SELECT id FROM datasets').fetchone()
        self.assertEqual(self.client.get('/v1/datasets/'+row['id'],headers={'Authorization':'Bearer other'}).status_code,404)
        self.assertEqual(self.client.post('/v1/datasets',headers=self.headers,files=[('files',('empty.csv',b''))]).status_code,422)

    def test_agent_schema_all_objects_require_all_properties(self):
        doc=schema()
        for obj in [doc,*doc['$defs'].values()]:
            self.assertFalse(obj['additionalProperties'])
            self.assertEqual(set(obj['required']),set(obj['properties']))

    def test_bad_graph_and_missing_finding_evidence_rejected(self):
        data=result('s');data['edges']=[dict(source='a',target='b',process='P',count=1,explanation='')]
        with self.assertRaises(ValueError):validate(data,{'s'})
        data=result('s');data['findings']=[dict(id='f',title='x',severity='low',category='x',explanation='x',case_ids=[],references=[],recommended_check='x')]
        with self.assertRaises(ValueError):validate(data,{'s'})

if __name__=='__main__':unittest.main()
