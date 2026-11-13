from contextlib import asynccontextmanager
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit
import asyncio
import json
import logging
import os
import secrets
import shutil
import sqlite3
import time
import uuid

import httpx
from fastapi import FastAPI, Request, UploadFile, File, HTTPException
from pydantic import BaseModel, Field, ConfigDict
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from agent import AgentError, review

AGENT=os.getenv('AGENT_API_URL','http://localhost:8081').rstrip('/')
WORKSPACE=os.getenv('WORKSPACE_API_URL','http://localhost:8090').rstrip('/')
DATA=Path(os.getenv('DATA_DIR','./data'))
DATA.mkdir(parents=True,exist_ok=True)
DB=DATA/'process_mining.sqlite3'
COOKIE='process_mining_session'
TASKS=set()
USER_LOCKS={}
MAX_FILE=40*1024*1024
LOGGER=logging.getLogger("uvicorn.error")


def db():
    conn=sqlite3.connect(DB,timeout=10)
    conn.row_factory=sqlite3.Row
    return conn


def initialize():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,guid TEXT NOT NULL,user_id TEXT NOT NULL,username TEXT NOT NULL,expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,guid TEXT NOT NULL,status TEXT NOT NULL,created REAL NOT NULL,payload TEXT NOT NULL,error TEXT NOT NULL DEFAULT '');
        """)
        conn.execute("CREATE TABLE IF NOT EXISTS datasets(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,created REAL NOT NULL,files TEXT NOT NULL)")
        conn.execute('DELETE FROM sessions WHERE expires<?',(time.time(),))
    DB.chmod(0o600)


def spawn(job_id):
    task=asyncio.create_task(worker(job_id));TASKS.add(task);task.add_done_callback(TASKS.discard)


@asynccontextmanager
async def lifespan(app):
    initialize()
    app.state.http=httpx.AsyncClient(timeout=httpx.Timeout(45,connect=8),follow_redirects=False)
    with db() as conn:
        pending=conn.execute("SELECT id FROM jobs WHERE status IN ('queued','running')").fetchall()
    for row in pending: spawn(row['id'])
    yield
    for task in list(TASKS): task.cancel()
    await asyncio.gather(*list(TASKS),return_exceptions=True)
    await app.state.http.aclose()


app=FastAPI(title='Process mining · pot zadeve',version='1.0.0',lifespan=lifespan)
app.mount('/static',StaticFiles(directory=Path(__file__).parent/'static'),name='static')


@app.middleware('http')
async def security(request,call_next):
    origin=request.headers.get('origin')
    expected=os.getenv('PUBLIC_ORIGIN')
    if request.method in {'POST','DELETE'} and origin:
        if (expected and origin.rstrip('/')!=expected.rstrip('/')) or (not expected and urlsplit(origin).netloc!=request.headers.get('host')):
            return JSONResponse({'detail':'Nedovoljen izvor zahteve.'},status_code=403)
    response=await call_next(request)
    response.headers['Cache-Control']='no-store'
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='no-referrer'
    response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    if request.url.path in ('/docs','/redoc'):
        del response.headers['Content-Security-Policy']
    return response


def session(request):
    key=sha256(request.cookies.get(COOKIE,'').encode()).hexdigest()
    with db() as conn:
        row=conn.execute('SELECT * FROM sessions WHERE id=? AND expires>?',(key,time.time())).fetchone()
    if row is None: raise HTTPException(401,'Prijavite se za nadaljevanje.')
    return dict(row)


async def upstream(request,method,base,path,guid=None,**kwargs):
    try:
        # Login needs expires_at; this adapter preserves the existing auth route contract.
        response=await request.app.state.http.request(method,base+path,params={'guid':guid} if guid else {},**kwargs)
    except httpx.RequestError:
        raise HTTPException(503,'Agent trenutno ni dosegljiv.') from None
    if response.status_code>=400:
        raise HTTPException(401 if response.status_code in (401,403) else 502,'Prijava ni veljavna.' if response.status_code in (401,403) else 'Agent zahteve ni mogel obdelati.')
    return response


async def authenticated(request):
    authorization=request.headers.get('authorization','')
    if authorization:
        scheme,_,guid=authorization.partition(' ')
        if scheme.lower()!='bearer' or not guid: raise HTTPException(401,'Uporabite Authorization: Bearer GUID.')
        me=(await upstream(request,'GET',AGENT,'/v1/auth/me',guid)).json()
        return {'user_id':me['user_id'],'username':me['username'],'guid':guid}
    return session(request)


@app.get('/')
async def index():
    return FileResponse(Path(__file__).parent / 'static/index.html')


@app.get('/health')
async def health():
    return {'status': 'ok'}


@app.post('/api/login')
async def login(request: Request):
    body = await request.json()
    guid = body.get('guid', '')
    expires = time.time() + 12 * 3600
    if not guid:
        response = await upstream(request, 'POST', AGENT, '/v1/auth/login', json={
            'username': body.get('username', ''), 'password': body.get('password', '')})
        payload = response.json()
        guid = payload['guid']
        expires = min(expires, datetime.fromisoformat(payload['expires_at'].replace('Z', '+00:00')).timestamp())
    me = (await upstream(request, 'GET', AGENT, '/v1/auth/me', guid)).json()
    token = secrets.token_urlsafe(32)
    with db() as conn:
        conn.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',
                     (sha256(token.encode()).hexdigest(), guid, me['user_id'], me['username'], expires))
    response = JSONResponse({'username': me['username']})
    response.set_cookie(COOKIE, token, httponly=True, secure=os.getenv('COOKIE_SECURE', 'false').lower() == 'true',
                        samesite='strict', max_age=max(0, int(expires-time.time())), path='/')
    return response


@app.get('/api/me')
async def me(request: Request):
    user = session(request)
    await upstream(request, 'GET', AGENT, '/v1/auth/me', user['guid'])
    return {'username': user['username']}


@app.post('/api/logout')
async def logout(request: Request):
    user = session(request)
    # Remove only this webservice session; other agent clients can keep working.
    with db() as conn:
        conn.execute('DELETE FROM sessions WHERE id=?', (user['id'],))
    response = JSONResponse({'ok': True})
    response.delete_cookie(COOKIE, path='/')
    return response


class AnalysisRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    dataset_id:str
    instructions:str=Field(default='',max_length=12000)


def dataset_row(id,owner):
    with db() as conn:row=conn.execute('SELECT * FROM datasets WHERE id=? AND user_id=?',(id,owner)).fetchone()
    if row is None:raise HTTPException(404,'Paket ne obstaja.')
    result=dict(row);result['files']=json.loads(result['files']);return result


def job_row(id,owner=None):
    with db() as conn:row=conn.execute('SELECT * FROM jobs WHERE id=?',(id,)).fetchone()
    if row is None or (owner is not None and row['user_id']!=owner):raise HTTPException(404,'Analiza ne obstaja.')
    result=dict(row);result['payload']=json.loads(result['payload']);return result


def save(job):
    with db() as conn:
        conn.execute('UPDATE jobs SET status=?,payload=?,error=?,guid=? WHERE id=?',
                     (job['status'],json.dumps(job['payload'],ensure_ascii=False),job['error'],job['guid'],job['id']))


def public(job,detail=False):
    p=job['payload'];item=p.get('review',{})
    result={'id':job['id'],'status':job['status'],'created':job['created'],'error':job['error'],
            'phase':item.get('phase',p.get('phase','V čakalni vrsti')) if job['status'] in ('queued','running') else p.get('phase',''),
            'dataset_id':p['dataset_id'],'agent_status':item.get('stage','waiting')}
    if detail:result['analysis']=p.get('analysis')
    return result


async def worker(job_id):
    job=job_row(job_id);p=job['payload']
    async with USER_LOCKS.setdefault(job['user_id'],asyncio.Lock()):
        job['status']='running';save(job)
        try:
            if not p.get('review'):
                dataset=dataset_row(p['dataset_id'],job['user_id'])
                p['review']={'id':job_id,'instructions':p['instructions'],'upload_files':[
                    {'id':f['id'],'path':f['path'],'name':f['id']+Path(f['name']).suffix.lower(),
                     'original_name':f['name'],'size':f['size'],
                     'visual':Path(f['name']).suffix.lower() in ('.pdf','.png','.jpg','.jpeg','.tif','.tiff','.webp')}
                    for f in dataset['files']]}
                save(job)
            p['phase']='Agent analizira datoteke in išče anomalije';save(job)
            p['analysis']=await review(app.state.http,AGENT,WORKSPACE,job['guid'],p['review'],lambda:save(job))
            p['phase']='Analiza je pripravljena';job.update(status='completed',guid='');save(job)
        except asyncio.CancelledError:
            save(job);raise
        except Exception as exc:
            LOGGER.error('Process analysis failed: job=%s error_type=%s',job_id,type(exc).__name__)
            job.update(status='failed',guid='',error=exc.message if isinstance(exc,AgentError) else 'Analize ni bilo mogoče dokončati.')
            p['phase']='Agentovo opravilo se je zaključilo z napako';save(job)


@app.post('/v1/datasets',status_code=201)
async def upload_dataset(request:Request,files:list[UploadFile]=File(...)):
    user=await authenticated(request)
    if not 1<=len(files)<=20:raise HTTPException(413,'Naložite od 1 do 20 datotek.')
    id=str(uuid.uuid4());folder=DATA/'datasets'/id;folder.mkdir(parents=True,mode=0o700)
    stored=[];total=0
    try:
        for file in files:
            name=Path((file.filename or 'file').replace('\\','/')).name
            if name in ('','.','..'):raise HTTPException(422,'Neveljavno ime datoteke.')
            data=await file.read(MAX_FILE+1);await file.close();total+=len(data)
            if not data:raise HTTPException(422,'Datoteka je prazna.')
            if len(data)>MAX_FILE or total>100*1024*1024:raise HTTPException(413,'Največ 40 MB na datoteko in 100 MB na paket.')
            fid=str(uuid.uuid4());path=folder/fid;path.write_bytes(data);path.chmod(0o600)
            stored.append({'id':fid,'name':name,'path':str(path),'size':len(data)})
        with db() as conn:conn.execute('INSERT INTO datasets VALUES(?,?,?,?)',(id,user['user_id'],time.time(),json.dumps(stored)))
    except Exception:
        shutil.rmtree(folder,ignore_errors=True);raise
    return {'id':id,'files':[{k:f[k] for k in ('id','name','size')} for f in stored]}


@app.get('/v1/datasets/{dataset_id}')
async def get_dataset(dataset_id:str,request:Request):
    user=await authenticated(request);d=dataset_row(dataset_id,user['user_id'])
    return {'id':dataset_id,'files':[{k:f[k] for k in ('id','name','size')} for f in d['files']]}


@app.post('/v1/analyses',status_code=202)
async def create_analysis(body:AnalysisRequest,request:Request):
    user=await authenticated(request);dataset_row(body.dataset_id,user['user_id'])
    id=str(uuid.uuid4());payload={'dataset_id':body.dataset_id,'instructions':body.instructions}
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute("SELECT id FROM jobs WHERE user_id=? AND status IN ('queued','running')",(user['user_id'],)).fetchone():raise HTTPException(409,'Za ta račun že teče analiza procesa.')
        conn.execute('INSERT INTO jobs(id,user_id,guid,status,created,payload) VALUES(?,?,?,?,?,?)',(id,user['user_id'],user['guid'],'queued',time.time(),json.dumps(payload)))
    spawn(id)
    return JSONResponse(public(job_row(id)),status_code=202,headers={'Location':'/v1/analyses/'+id})


@app.get('/v1/analyses')
async def list_analyses(request:Request):
    user=await authenticated(request)
    with db() as conn:rows=conn.execute('SELECT id FROM jobs WHERE user_id=? ORDER BY created DESC LIMIT 30',(user['user_id'],)).fetchall()
    return [public(job_row(r['id'])) for r in rows]


@app.get('/v1/analyses/{id}')
async def get_analysis(id:str,request:Request):
    user=await authenticated(request)
    return public(job_row(id,user['user_id']),True)


@app.get('/v1/schema')
async def response_schema():
    from report import schema
    return schema()


def custom_openapi():
    if app.openapi_schema:return app.openapi_schema
    from fastapi.openapi.utils import get_openapi
    spec=get_openapi(title=app.title,version=app.version,routes=app.routes)
    spec.setdefault('components',{}).setdefault('securitySchemes',{})['AgentGuid']={
        'type':'http','scheme':'bearer','description':'Veljaven agentov GUID; GUI uporablja prijavni piškotek.'}
    for path,methods in spec['paths'].items():
        if path.startswith('/v1/') and path!='/v1/schema':
            for method,operation in methods.items():
                if method in ('get','post','delete'):operation['security']=[{'AgentGuid':[]}]
    app.openapi_schema=spec
    return spec


app.openapi=custom_openapi
