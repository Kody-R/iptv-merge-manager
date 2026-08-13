from __future__ import annotations

import asyncio, os, shutil, uuid, zipfile, tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfo
from difflib import SequenceMatcher

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from lxml import etree

from .db import connect, init_db, rows_to_dicts, DB_PATH
from .iptv import generate_outputs, refresh_source, normalize_name

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path('/app/data'); UPLOAD_DIR = DATA_DIR/'uploads'; CACHE_DIR = DATA_DIR/'cache'; OUTPUT_DIR=Path('/app/output')
TZ_NAME=os.getenv('TZ','America/New_York'); REFRESH_HOURS=max(1,int(os.getenv('REFRESH_HOURS','4')))
refresh_lock=asyncio.Lock(); scheduler=AsyncIOScheduler(timezone=ZoneInfo(TZ_NAME))

async def refresh_all():
    async with refresh_lock:
        with connect() as conn: ids=[r['id'] for r in conn.execute('SELECT id FROM sources WHERE enabled=1 ORDER BY id')]
        results=[]
        for sid in ids:
            try: results.append(await refresh_source(sid))
            except Exception as e: results.append({'source_id':sid,'status':'error','error':str(e)})
        return {'sources':results,'output':generate_outputs()}

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(); generate_outputs()
    scheduler.add_job(refresh_all,CronTrigger(hour=f'*/{REFRESH_HOURS}',minute=0,timezone=ZoneInfo(TZ_NAME)),id='refresh-all',replace_existing=True,coalesce=True,max_instances=1)
    scheduler.start(); yield; scheduler.shutdown(wait=False)

app=FastAPI(title='IPTV Merge Manager',version='0.2.0',lifespan=lifespan)
app.mount('/static',StaticFiles(directory=APP_DIR/'static'),name='static'); templates=Jinja2Templates(directory=APP_DIR/'templates')

class ChannelPatch(BaseModel):
    selected: bool|None=None; channel_number:int|None=None
class ChannelEdit(BaseModel):
    custom_name:str|None=None; custom_group:str|None=None; custom_tvg_id:str|None=None; custom_logo:str|None=None; channel_number:int|None=None
class BulkAction(BaseModel):
    ids:list[int]; action:str; value:str|int|None=None
class ReorderRequest(BaseModel): ids:list[int]
class AutoNumberRequest(BaseModel): start:int=1; increment:int=1; mode:str='sequential'
class GroupCreate(BaseModel): name:str; number_start:int=1; number_increment:int=1
class GroupPatch(BaseModel): name:str|None=None; number_start:int|None=None; number_increment:int|None=None

@app.get('/',response_class=HTMLResponse)
def index(request:Request): return templates.TemplateResponse('index.html',{'request':request,'refresh_hours':REFRESH_HOURS})
@app.get('/health')
def health(): return {'status':'ok','version':'0.2.0'}


def _xml_ids():
    ids=set()
    for path in CACHE_DIR.glob('source_*.xml'):
        try:
            for _,e in etree.iterparse(str(path),events=('end',),tag='channel',recover=True,huge_tree=True):
                if e.get('id'): ids.add(e.get('id'))
                e.clear()
        except Exception: pass
    return ids

@app.get('/api/status')
def status():
    xml_ids=_xml_ids()
    with connect() as c:
        source_count=c.execute('SELECT COUNT(*) FROM sources').fetchone()[0]; active=c.execute('SELECT COUNT(*) FROM channels WHERE is_active=1').fetchone()[0]
        selected=c.execute('SELECT COUNT(*) FROM channels WHERE selected=1 AND is_active=1').fetchone()[0]
        rows=c.execute("SELECT COALESCE(custom_tvg_id,tvg_id) tid FROM channels WHERE selected=1 AND is_active=1").fetchall()
        with_epg=sum(1 for r in rows if r['tid'] and r['tid'] in xml_ids); missing=selected-with_epg
        groups=c.execute("SELECT COUNT(DISTINCT COALESCE(NULLIF(custom_group,''),NULLIF(group_title,''))) FROM channels WHERE selected=1 AND is_active=1").fetchone()[0]
        logs=rows_to_dicts(c.execute("SELECT l.*,s.name source_name FROM refresh_log l LEFT JOIN sources s ON s.id=l.source_id ORDER BY l.id DESC LIMIT 20").fetchall())
        unnumbered=c.execute('SELECT COUNT(*) FROM channels WHERE selected=1 AND is_active=1 AND channel_number IS NULL').fetchone()[0]
    return {'version':'0.2.0','timezone':TZ_NAME,'refresh_hours':REFRESH_HOURS,'source_count':source_count,'active_channels':active,'selected_channels':selected,'with_epg':with_epg,'missing_epg':missing,'group_count':groups,'unnumbered':unnumbered,'logs':logs}

@app.get('/api/sources')
def sources():
    with connect() as c:return rows_to_dicts(c.execute('SELECT * FROM sources ORDER BY id').fetchall())

@app.post('/api/sources')
async def add_source(name:str=Form(...),m3u_url:str|None=Form(None),xml_url:str|None=Form(None),m3u_file:UploadFile|None=File(None),xml_file:UploadFile|None=File(None)):
    name=name.strip(); m3u_url=(m3u_url or '').strip() or None; xml_url=(xml_url or '').strip() or None
    if m3u_file and not m3u_file.filename:m3u_file=None
    if xml_file and not xml_file.filename:xml_file=None
    if not name: raise HTTPException(400,'Source name is required')
    if not m3u_url and not m3u_file: raise HTTPException(400,'Provide an M3U URL or file')
    if m3u_url and m3u_file: raise HTTPException(400,'Use either M3U URL or upload')
    if xml_url and xml_file: raise HTTPException(400,'Use either XMLTV URL or upload')
    UPLOAD_DIR.mkdir(parents=True,exist_ok=True)
    if m3u_file:
        path=UPLOAD_DIR/f'{uuid.uuid4().hex}{Path(m3u_file.filename).suffix or ".m3u"}'; path.write_bytes(await m3u_file.read()); mk,mv='file',str(path)
    else: mk,mv='url',m3u_url
    xk=xv=None
    if xml_file:
        path=UPLOAD_DIR/f'{uuid.uuid4().hex}{"".join(Path(xml_file.filename).suffixes) or ".xml"}'; path.write_bytes(await xml_file.read()); xk,xv='file',str(path)
    elif xml_url:xk,xv='url',xml_url
    with connect() as c:sid=c.execute('INSERT INTO sources(name,m3u_kind,m3u_value,xml_kind,xml_value) VALUES(?,?,?,?,?)',(name,mk,mv,xk,xv)).lastrowid
    try:
        async with refresh_lock:r=await refresh_source(sid); generate_outputs()
        return {'id':sid,'refresh':r}
    except Exception as e:return {'id':sid,'refresh':{'status':'error','error':str(e)}}

@app.delete('/api/sources/{sid}')
def delete_source(sid:int):
    with connect() as c:
        if not c.execute('SELECT 1 FROM sources WHERE id=?',(sid,)).fetchone():raise HTTPException(404,'Source not found')
        c.execute('DELETE FROM sources WHERE id=?',(sid,))
    generate_outputs(); return {'ok':True}
@app.post('/api/sources/{sid}/refresh')
async def refresh_one(sid:int):
    async with refresh_lock:r=await refresh_source(sid); return {'refresh':r,'output':generate_outputs()}
@app.post('/api/refresh')
async def refresh_everything(): return await refresh_all()

@app.get('/api/channels')
def channels(source_id:int|None=None,group:str|None=None,q:str|None=None,selected:bool|None=None,active:bool=True):
    sql="""SELECT c.*,s.name source_name,COALESCE(NULLIF(c.custom_name,''),c.name) display_name,COALESCE(NULLIF(c.custom_group,''),c.group_title) display_group,COALESCE(NULLIF(c.custom_tvg_id,''),c.tvg_id) display_tvg_id,COALESCE(NULLIF(c.custom_logo,''),c.logo) display_logo FROM channels c JOIN sources s ON s.id=c.source_id WHERE 1=1"""; p=[]
    if active:sql+=' AND c.is_active=1'
    if source_id is not None:sql+=' AND c.source_id=?';p.append(source_id)
    if group:sql+=" AND COALESCE(NULLIF(c.custom_group,''),c.group_title,'')=?";p.append(group)
    if q:
        like=f'%{q}%';sql+=" AND (c.name LIKE ? OR COALESCE(c.custom_name,'') LIKE ? OR COALESCE(c.tvg_id,'') LIKE ? OR s.name LIKE ?)";p += [like]*4
    if selected is not None:sql+=' AND c.selected=?';p.append(1 if selected else 0)
    sql+=' ORDER BY CASE WHEN c.selected=1 THEN 0 ELSE 1 END,c.sort_order,c.name COLLATE NOCASE'
    with connect() as c:return rows_to_dicts(c.execute(sql,p).fetchall())

@app.patch('/api/channels/{cid}')
def patch_channel(cid:int,patch:ChannelPatch):
    f=[];p=[]
    if patch.selected is not None:f+=['selected=?'];p+=[1 if patch.selected else 0]
    if 'channel_number' in patch.model_fields_set:f+=['channel_number=?'];p+=[patch.channel_number]
    if f:
        with connect() as c:c.execute(f"UPDATE channels SET {','.join(f)} WHERE id=?",[*p,cid])
        generate_outputs()
    return {'ok':True}
@app.put('/api/channels/{cid}/edit')
def edit_channel(cid:int,e:ChannelEdit):
    vals=[(x.strip() if isinstance(x,str) else x) or None for x in (e.custom_name,e.custom_group,e.custom_tvg_id,e.custom_logo)]
    with connect() as c:
        c.execute('UPDATE channels SET custom_name=?,custom_group=?,custom_tvg_id=?,custom_logo=?,channel_number=? WHERE id=?',(*vals,e.channel_number,cid))
    generate_outputs();return {'ok':True}

@app.post('/api/channels/bulk')
def bulk(req:BulkAction):
    if not req.ids:return {'updated':0}
    ph=','.join('?'*len(req.ids)); field=None; value=req.value
    if req.action=='enable':field='selected';value=1
    elif req.action=='disable':field='selected';value=0
    elif req.action=='group':field='custom_group';value=str(value or '').strip() or None
    elif req.action=='clear-numbers':field='channel_number';value=None
    else:raise HTTPException(400,'Unknown bulk action')
    with connect() as c:cur=c.execute(f'UPDATE channels SET {field}=? WHERE id IN ({ph})',[value,*req.ids])
    generate_outputs();return {'updated':cur.rowcount}

@app.post('/api/lineup/reorder')
def reorder(req:ReorderRequest):
    with connect() as c:
        for i,cid in enumerate(req.ids,1):c.execute('UPDATE channels SET sort_order=? WHERE id=? AND selected=1',(i*10,cid))
    generate_outputs();return {'ok':True}
@app.post('/api/lineup/autonumber')
def autonumber(req:AutoNumberRequest):
    with connect() as c:
        rows=c.execute('SELECT id,channel_number FROM channels WHERE selected=1 AND is_active=1 ORDER BY sort_order,id').fetchall(); n=req.start
        if req.mode=='fill-gaps':
            used={r['channel_number'] for r in rows if r['channel_number'] is not None}
            for r in rows:
                if r['channel_number'] is not None:continue
                while n in used:n+=req.increment
                c.execute('UPDATE channels SET channel_number=? WHERE id=?',(n,r['id']));used.add(n);n+=req.increment
        else:
            for r in rows:c.execute('UPDATE channels SET channel_number=? WHERE id=?',(n,r['id']));n+=req.increment
    generate_outputs();return {'ok':True}

@app.get('/api/groups')
def groups():
    with connect() as c:
        configured=rows_to_dicts(c.execute('SELECT * FROM lineup_groups ORDER BY sort_order,id').fetchall())
        names=[r[0] for r in c.execute("SELECT DISTINCT COALESCE(NULLIF(custom_group,''),group_title) g FROM channels WHERE is_active=1 AND g IS NOT NULL AND TRIM(g)<>'' ORDER BY g COLLATE NOCASE").fetchall()]
    return {'configured':configured,'names':names}
@app.post('/api/groups')
def create_group(g:GroupCreate):
    with connect() as c:
        order=c.execute('SELECT COALESCE(MAX(sort_order),0)+10 FROM lineup_groups').fetchone()[0]
        c.execute('INSERT INTO lineup_groups(name,sort_order,number_start,number_increment) VALUES(?,?,?,?)',(g.name.strip(),order,g.number_start,g.number_increment))
    return {'ok':True}
@app.delete('/api/groups/{gid}')
def delete_group(gid:int):
    with connect() as c:c.execute('DELETE FROM lineup_groups WHERE id=?',(gid,))
    return {'ok':True}
@app.post('/api/groups/autonumber')
def group_autonumber():
    with connect() as c:
        gs=c.execute('SELECT * FROM lineup_groups ORDER BY sort_order,id').fetchall()
        for g in gs:
            rows=c.execute("SELECT id FROM channels WHERE selected=1 AND is_active=1 AND COALESCE(NULLIF(custom_group,''),group_title)=? ORDER BY sort_order,id",(g['name'],)).fetchall();n=g['number_start']
            for r in rows:c.execute('UPDATE channels SET channel_number=? WHERE id=?',(n,r['id']));n+=g['number_increment']
    generate_outputs();return {'ok':True}

@app.get('/api/channels/{cid}/epg-suggestions')
def epg_suggestions(cid:int):
    with connect() as c:r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not r:raise HTTPException(404,'Channel not found')
    target=normalize_name(r['custom_name'] or r['name']); suggestions=[]
    for path in CACHE_DIR.glob('source_*.xml'):
        try:
            sid=int(path.stem.split('_')[1])
            with connect() as c:sname=(c.execute('SELECT name FROM sources WHERE id=?',(sid,)).fetchone() or ['Unknown'])[0]
            for _,e in etree.iterparse(str(path),events=('end',),tag='channel',recover=True,huge_tree=True):
                eid=e.get('id'); names=[''.join(x.itertext()).strip() for x in e.findall('display-name')]
                best=max([SequenceMatcher(None,target,normalize_name(n)).ratio() for n in names] or [0])
                if eid and best>=.45:suggestions.append({'tvg_id':eid,'name':names[0] if names else eid,'source_name':sname,'score':round(best*100)})
                e.clear()
        except Exception:pass
    suggestions.sort(key=lambda x:x['score'],reverse=True);return suggestions[:10]

@app.get('/api/backup')
def backup():
    out=OUTPUT_DIR/'iptv-merge-manager-backup.zip'; OUTPUT_DIR.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        if DB_PATH.exists():z.write(DB_PATH,'data/iptv.db')
        for root in (UPLOAD_DIR,):
            if root.exists():
                for p in root.rglob('*'):
                    if p.is_file():z.write(p,'data/uploads/'+p.name)
    return FileResponse(out,media_type='application/zip',filename='iptv-merge-manager-backup.zip')
@app.post('/api/restore')
async def restore(file:UploadFile=File(...)):
    data=await file.read()
    with tempfile.TemporaryDirectory() as td:
        zp=Path(td)/'b.zip';zp.write_bytes(data)
        try:
            with zipfile.ZipFile(zp) as z:
                names=set(z.namelist())
                if 'data/iptv.db' not in names:raise HTTPException(400,'Backup does not contain data/iptv.db')
                z.extractall(td)
                shutil.copy2(Path(td)/'data/iptv.db',DB_PATH)
                up=Path(td)/'data/uploads'
                if up.exists():
                    UPLOAD_DIR.mkdir(parents=True,exist_ok=True)
                    for p in up.iterdir():
                        if p.is_file():shutil.copy2(p,UPLOAD_DIR/p.name)
        except zipfile.BadZipFile:raise HTTPException(400,'Invalid backup ZIP')
    init_db();generate_outputs();return {'ok':True}

@app.get('/output/master.m3u')
def m3u():
    p=OUTPUT_DIR/'master.m3u';
    if not p.exists():generate_outputs()
    return FileResponse(p,media_type='audio/x-mpegurl',filename='master.m3u')
@app.get('/output/master.xml')
def xml():
    p=OUTPUT_DIR/'master.xml';
    if not p.exists():generate_outputs()
    return FileResponse(p,media_type='application/xml',filename='master.xml')
