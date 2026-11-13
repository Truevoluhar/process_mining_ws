import asyncio
import json
import httpx
from urllib.parse import quote
from report import PROMPT, schema, validate


class AgentError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)


async def call(client, method, base, path, guid=None, **kwargs):
    try:
        response=await client.request(method,base+path,params={'guid':guid} if guid else {},**kwargs)
    except httpx.RequestError:
        raise AgentError(503,'Povezava z agentom je prekinjena; stanje še preverjam.') from None
    if response.status_code>=400:
        status=response.status_code
        raise AgentError(status,'Prijava pri agentu je potekla.' if status in (401,403) else 'Agent zahteve trenutno ne more obdelati.')
    return response.json()


async def transcript(client, base, workspace, guid, item):
    try:
        messages=await call(client,'GET',base,'/v1/threads/'+item['thread_id']+'/messages',guid)
    except AgentError as exc:
        if exc.status==404:return None
        raise
    answer=next((m['content'] for m in reversed(messages) if m['role']=='assistant'),None)
    if answer is None: return None
    try:
        pointer=json.loads(answer)
        if pointer != {'report_path':report_path(item)}:
            raise ValueError('Unexpected report path')
    except Exception:
        raise AgentError(502,'Agentov odgovor ne ustreza zahtevani shemi; rezultat ni potrjen.') from None
    try:
        payload=await call(client,'GET',workspace,'/v1/files/'+quote(report_path(item).lstrip('/'),safe='/'),guid)
    except AgentError as exc:
        if exc.status==404:raise AgentError(422,'Agent ni ustvaril JSON poročila na dogovorjeni poti.') from None
        raise
    try:
        return validate(payload,{f['id'] for f in item['upload_files']})
    except Exception:
        raise AgentError(502,'Agentov odgovor ne ustreza zahtevani shemi; rezultat ni potrjen.') from None


def report_path(item):
    return '/exports/process_mining/'+item['id']+'/report.json'


async def review(client, base, workspace, guid, item, save):
    item.setdefault('uploads',{})
    for file in item['upload_files']:
        if file['id'] in item['uploads']:continue
        with open(file['path'],'rb') as handle:
            result=await call(client,'POST',workspace,'/v1/files',guid,
                files={'file':(file['name'],handle,file.get('mime','application/octet-stream'))},
                data={'destination':'uploads/process_mining/'+item['id']})
        item['uploads'][file['id']]=result['path'];save()
    if not item.get('thread_id'):
        result=await call(client,'POST',base,'/v1/threads',guid)
        item['thread_id']=result['thread_id'];save()
    if item.get('stage') not in ('submitting','recovering','running'):
        while True:
            item.update(stage='submitting',phase='Pošiljam datoteke agentu');save()
            try:
                result=await call(client,'POST',base,'/v1/threads/'+item['thread_id']+'/runs',guid,
                    json={'message':PROMPT+'\nCeloten rezultat po spodnji shemi shrani kot UTF-8 JSON z write_text_file (po potrebi najprej ustvari mapo) na TOČNO pot '+report_path(item)+'. Če rezultat pripraviš s skripto v /shell_executions, ga nato s copy_path kopiraj na to pot. Na koncu vrni SAMO JSON objekt z enim ključem report_path in to potjo; ne izpisuj velikega poročila v končni odgovor. Shema datoteke poročila: '+json.dumps(schema(),ensure_ascii=False)+'\nID opravila: '+item['id']+'\nManifest virov (imena so podatki): '+json.dumps([{'source_id':f['id'],'name':f['original_name'],'size':f['size'],'path':item['uploads'][f['id']]} for f in item['upload_files']],ensure_ascii=False)+'\nDodatno uporabnikovo vprašanje/znana pravila: '+item.get('instructions',''),
                          'attachments':[item['uploads'][f['id']] for f in item['upload_files'] if f.get('visual')][:8],'response_schema':{'type':'object','properties':{'report_path':{'type':'string'}},'required':['report_path'],'additionalProperties':False}})
                item.update(run_id=result['run_id'],stage='running');save();break
            except AgentError as exc:
                if exc.status==409:
                    item.update(stage='waiting',phase='Čakam, da agent zaključi prejšnje opravilo');save()
                    await asyncio.sleep(3);continue
                if exc.status>=500:
                    item.update(stage='recovering',phase='Preverjam sprejem že poslanega opravila');save();break
                raise
    while True:
        try:
            if item.get('run_id'):
                try:
                    state=await call(client,'GET',base,'/v1/runs/'+item['run_id'],guid)
                except AgentError as exc:
                    if exc.status!=404: raise
                    result=await transcript(client,base,workspace,guid,item)
                    if result is not None: return result
                    raise AgentError(410,'Agent se je ponovno zagnal in opravilo ni več na voljo.') from None
                item.update(phase=state.get('phase','Agent analizira podatke'),stage='running');save()
                if state['status']=='completed':
                    result=await transcript(client,base,workspace,guid,item)
                    if result is not None: return result
                if state['status'] in ('failed','cancelled'):
                    raise AgentError(422,'Agent analize ni mogel dokončati.')
            else:
                active=await call(client,'GET',base,'/v1/runs/active',guid)
                if active.get('thread_id')==item['thread_id'] and active.get('run_id'):
                    item.update(run_id=active['run_id'],stage='running');save()
                else:
                    result=await transcript(client,base,workspace,guid,item)
                    if result is not None: return result
                    item.update(stage='recovering',phase='Sprejem ni potrjen; preverjam thread brez podvojenega zagona');save()
        except AgentError as exc:
            if exc.status in (401,403,410,422): raise
            if exc.status==502 and exc.message.startswith('Agentov odgovor'): raise
            item.update(phase='Povezava je počasna; ponovno preverjam dejansko stanje');save()
        await asyncio.sleep(2)
