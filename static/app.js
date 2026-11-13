'use strict';
const $=id=>document.getElementById(id);
let selected='', report=null, rendered='', downloadURL='', polling=false, busy=false, signedIn=false;
const active=s=>s==='queued'||s==='running';
function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
function clear(id){$(id).replaceChildren();return $(id);}
function notice(text){$('notice').textContent=text;$('notice').hidden=!text;}
async function api(path,options={}){
 const controller=new AbortController(), timer=setTimeout(()=>controller.abort(),options.body instanceof FormData?180000:25000);
 try {const r=await fetch(path,{...options,signal:controller.signal});const b=await r.json();if(!r.ok){if(r.status===401)auth(false);throw new Error(typeof b.detail==='string'?b.detail:JSON.stringify(b.detail));}return b;}
 catch(e){if(e.name==='AbortError')throw new Error('Povezava je počasna. Spremljanje se nadaljuje; stanje opravila še ni potrjeno.');throw e;}
 finally{clearTimeout(timer);}
}
function auth(ok,name=''){signedIn=ok;$('login-panel').hidden=ok;$('workspace').hidden=!ok;$('account').hidden=!ok;$('username').textContent=name;if(!ok){selected='';report=null;rendered='';$('dashboard').hidden=true;$('status').hidden=true;if(downloadURL)URL.revokeObjectURL(downloadURL);$('download').hidden=true;}}
for(const id of ['login','token-login'])$(id).addEventListener('submit',async e=>{e.preventDefault();const b=e.submitter;b.disabled=true;try{const result=await api('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.fromEntries(new FormData(e.target)))});e.target.reset();auth(true,result.username);notice('');await refresh();}catch(err){notice(err.message);}finally{b.disabled=false;}});
$('logout').onclick=async()=>{try{await api('/api/logout',{method:'POST'});auth(false);notice('');}catch(e){notice(e.message);}};
$('upload').onsubmit=async e=>{e.preventDefault();if(busy)return;busy=true;$('analyze').disabled=true;notice('Nalaganje datotek …');try{
 const files=new FormData();for(const file of $('files').files)files.append('files',file);
 const dataset=await api('/v1/datasets',{method:'POST',body:files});
 const job=await api('/v1/analyses',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dataset_id:dataset.id,instructions:$('instructions').value})});
 selected=job.id;rendered='';report=null;$('dashboard').hidden=true;$('new-analysis').open=false;notice('');await refresh();
 }catch(err){notice(err.message+' Če je bil zagon že sprejet, se bo opravilo pojavilo na seznamu analiz.');await refresh();}finally{busy=false;$('analyze').disabled=false;}};
$('analysis-picker').onchange=async()=>{selected=$('analysis-picker').value;rendered='';report=null;$('dashboard').hidden=true;await refresh();};
$('refresh').onclick=()=>refresh();
async function refresh(){
 if(polling||!signedIn)return;polling=true;
 try{
  const jobs=await api('/v1/analyses');if(!signedIn)return;
  const picker=clear('analysis-picker');if(!selected||!jobs.some(j=>j.id===selected))selected=jobs[0]?.id||'';
  for(const j of jobs){const o=el('option',new Date(j.created*1000).toLocaleString('sl-SI')+' · '+j.status);o.value=j.id;picker.append(o);}picker.value=selected;
  if(!selected){picker.append(el('option','Ni še analiz'));return;}
  const id=selected, job=await api('/v1/analyses/'+encodeURIComponent(id));if(id!==selected||!signedIn)return;
  const status=clear('status');status.hidden=false;const row=el('div',undefined,'status-bar');if(active(job.status))row.append(el('span',undefined,'pulse'));
  row.append(el('strong',active(job.status)?'Agent izvaja opravilo …':job.status==='completed'?'Analiza pripravljena':'Opravilo se je zaključilo z napako'));status.append(row,el('p',job.phase));
  if(job.error)status.append(el('p',job.error));
  status.append(el('small','Stanje preverjeno: '+new Date().toLocaleTimeString('sl-SI')+' · ID: '+job.id));
  if(job.analysis&&rendered!==job.id){rendered=job.id;report=job.analysis;renderReport();}
 }catch(e){if(signedIn){$('status').hidden=false;const n=el('p','Stanja trenutno ne morem osvežiti: '+e.message+' Zadnja potrjena analiza ostaja prikazana. Opravilo s tem ni označeno za končano.');clear('status').append(n);}}
 finally{polling=false;}
}
function tab(name){document.querySelectorAll('[data-tab]').forEach(b=>b.classList.toggle('selected',b.dataset.tab===name));for(const t of ['overview','map','variants','anomalies','cases'])$('tab-'+t).hidden=t!==name;}
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>tab(b.dataset.tab));
function refs(parent,items){for(const r of items){const s=report.sources.find(x=>x.source_id===r.source_id);parent.append(el('p',(s?s.role+' · ':'')+r.source_id+' · '+r.locator,'meta'));}}
function caseButton(id){const b=el('button',id,'quiet');b.onclick=()=>showCase(id);return b;}
function list(parent,items){const ul=el('ul');for(const text of items)ul.append(el('li',text));parent.append(ul);}
function renderReport(){
 $('dashboard').hidden=false;clear('metrics');for(const m of report.metrics){const c=el('div',undefined,'metric');c.append(el('strong',m.value),el('span',m.label),el('small',m.explanation));$('metrics').append(c);}
 $('coverage').textContent=report.coverage;
 const overview=clear('agent-report');overview.append(el('h2','Agentova analiza'),el('p',report.summary),el('h3','Predlagani ukrepi'));list(overview,report.recommendations);
 const limits=clear('limitations');list(limits,report.limitations);for(const s of report.sources){limits.append(el('h3',s.role),el('p',s.source_id,'code'),el('p',s.coverage),el('small','Prebrane vrstice: '+(s.rows_read??'neznano')+' / '+(s.rows_total??'neznano')));}
 const cohort=clear('cohort');cohort.append(el('option','Vsi procesi'));cohort.firstChild.value='';
 const processes=new Set([...report.nodes,...report.cases,...report.variants].map(x=>x.process));for(const p of processes){const o=el('option',p);o.value=p;cohort.append(o);}
 if(downloadURL)URL.revokeObjectURL(downloadURL);downloadURL=URL.createObjectURL(new Blob([JSON.stringify(report,null,2)],{type:'application/json'}));$('download').href=downloadURL;$('download').download='process-analysis-'+selected+'.json';$('download').hidden=false;
 renderViews();renderFindings();
}
function matches(item){return !$('cohort').value||item.process===$('cohort').value;}
function trace(path){const row=el('div',undefined,'trace');for(const step of path)row.append(el('span',step));return row;}
function renderViews(){if(!report)return;drawGraph();const variants=clear('variants');for(const v of report.variants.filter(matches)){const card=el('div',undefined,'variant');card.append(el('strong',v.process),trace(v.path),el('p','Pojavitev: '+(v.count??'neznano')+' · Delež: '+v.share));const links=el('div',undefined,'links');for(const c of v.case_ids)links.append(caseButton(c));card.append(links);variants.append(card);}if(!variants.childNodes.length)variants.append(el('p','Agent ni vrnil različic poti za ta prikaz.'));renderCases();$('case-detail').hidden=true;}
$('cohort').onchange=renderViews;
$('severity').onchange=renderFindings;
function renderFindings(){if(!report)return;const container=clear('anomalies');for(const f of report.findings.filter(x=>!$('severity').value||x.severity===$('severity').value)){const card=el('article',undefined,'evidence');card.append(el('span',f.severity,'badge '+f.severity),el('span',f.category,'badge'),el('h3',f.id+' · '+f.title),el('p',f.explanation),el('strong','Predlagano preverjanje'),el('p',f.recommended_check));refs(card,f.references);const links=el('div',undefined,'links');for(const c of f.case_ids)links.append(caseButton(c));card.append(links);container.append(card);}if(!container.childNodes.length)container.append(el('p','Ni ugotovitev za izbrani filter.'));}
$('case-query').oninput=renderCases;
function renderCases(){if(!report)return;const container=clear('case-list'),q=$('case-query').value.toLowerCase();for(const c of report.cases.filter(matches).filter(c=>(c.case_id+' '+c.events.map(e=>e.activity).join(' ')).toLowerCase().includes(q))){const row=el('div',undefined,'variant');row.append(caseButton(c.case_id),el('span',c.process,'badge'),el('p',c.summary),el('small',c.coverage));container.append(row);}if(!container.childNodes.length)container.append(el('p','V agentovem izboru ni ustreznih zadev.'));}
function showCase(id){const c=report.cases.find(x=>x.case_id===id);tab('cases');const detail=clear('case-detail');detail.hidden=false;detail.append(el('h2','Pot zadeve '+id));if(!c){detail.append(el('p','Ta zadeva ni vključena v agentov izbor prikazanih poti. ID preverite v izvornih podatkih.'));return;}
 detail.append(el('p',c.process+' · '+c.summary),el('p',c.coverage,'notice'),trace(c.events.map(e=>e.activity)));const timeline=el('div',undefined,'timeline');for(const e of c.events){const item=el('article',undefined,'event');item.append(el('h4',e.activity),el('p',(e.timestamp??'Čas neznan')+' · Izvajalec: '+(e.actor??'neznan'),'meta'),el('p',e.observation));refs(item,e.references);timeline.append(item);}detail.append(timeline);detail.scrollIntoView({behavior:'smooth',block:'start'});
}
function svg(tag,attrs={},text){const n=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [k,v]of Object.entries(attrs))n.setAttribute(k,String(v));if(text!==undefined)n.textContent=text;return n;}
function drawGraph(){const box=clear('graph'),nodes=report.nodes.filter(matches),edges=report.edges.filter(matches);if(!nodes.length){box.append(el('p','Agent ni vrnil zemljevida procesa.'));return;}
 const positions=new Map(),rows=Math.ceil(nodes.length/4),height=rows*140+70;
 const root=svg('svg',{viewBox:'0 0 1000 '+height,role:'img','aria-label':'Agentov zemljevid procesa'}),defs=svg('defs'),marker=svg('marker',{id:'arrow',viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:6,markerHeight:6,orient:'auto-start-reverse'});marker.append(svg('path',{d:'M 0 0 L 10 5 L 0 10 z',fill:'#9aafc1'}));defs.append(marker);root.append(defs);
 nodes.forEach((n,i)=>positions.set(n.id,{x:45+(i%4)*245,y:45+Math.floor(i/4)*140}));
 for(const e of edges){const a=positions.get(e.source),b=positions.get(e.target);if(!a||!b)continue;const self=e.source===e.target,d=self?`M ${a.x+80} ${a.y} C ${a.x+20} ${a.y-40} ${a.x+180} ${a.y-40} ${a.x+140} ${a.y}`:`M ${a.x+100} ${a.y+55} C ${a.x+100} ${a.y+95} ${b.x+100} ${b.y-40} ${b.x+100} ${b.y}`;const path=svg('path',{d,fill:'none',stroke:'#9aafc1','stroke-width':1.7,'marker-end':'url(#arrow)'});path.append(svg('title',{},'Prehod: '+(e.count??'neznano')+' · '+e.explanation));root.append(path);}
 for(const n of nodes){const p=positions.get(n.id),g=svg('g',{tabindex:0,role:'button','aria-label':n.label});g.append(svg('rect',{x:p.x,y:p.y,width:205,height:62,rx:10,fill:'#e9f1f8',stroke:'#b8cbdb'}),svg('text',{x:p.x+12,y:p.y+25,fill:'#244c6b','font-size':13},n.label.length>25?n.label.slice(0,24)+'…':n.label),svg('text',{x:p.x+12,y:p.y+46,fill:'#637b8d','font-size':11},n.process+' · '+(n.count??'?')),svg('title',{},n.label));const open=()=>{$('case-query').value=n.label;renderCases();tab('cases');};g.addEventListener('click',open);g.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();open();}});root.append(g);}box.append(root);
 const legend=el('div',undefined,'links');for(const e of edges){const a=report.nodes.find(n=>n.id===e.source),b=report.nodes.find(n=>n.id===e.target);const line=el('p',(a?.label??e.source)+' → '+(b?.label??e.target)+' · '+(e.count??'?')+' · '+e.explanation,'meta');legend.append(line);}box.append(legend);
}
setInterval(()=>refresh(),3000);
(async()=>{try{const user=await api('/api/me');auth(true,user.username);await refresh();}catch(e){auth(false);if(!e.message.includes('Prijavite se'))notice(e.message);}})();
