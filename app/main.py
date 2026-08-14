from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .db import DB_PATH, connect, get_setting, init_db, rows_to_dicts, set_setting

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
DATA_DIR = Path('/app/data')
UPLOAD_DIR = DATA_DIR / 'uploads'
CACHE_DIR = DATA_DIR / 'cache'
OUTPUT_DIR = Path('/app/output')
TZ_NAME = os.getenv('TZ', 'America/New_York')
REFRESH_HOURS = max(1, int(os.getenv('REFRESH_HOURS', '4')))
UPLOAD_CHUNK = 256 * 1024
refresh_lock = asyncio.Lock()
scheduler = AsyncIOScheduler(timezone=ZoneInfo(TZ_NAME))

PROFILES = {
    'low-memory': {'page_size': 100, 'history_limit': 10, 'label': 'Low Memory'},
    'balanced': {'page_size': 250, 'history_limit': 30, 'label': 'Balanced'},
    'performance': {'page_size': 500, 'history_limit': 100, 'label': 'Performance'},
}


def profile_config() -> tuple[str, dict]:
    name = get_setting('resource_profile', 'low-memory')
    if name not in PROFILES:
        name = 'low-memory'
    return name, PROFILES[name]


def run_worker(action: str, *args: object) -> dict:
    cmd = [sys.executable, '-m', 'app.worker', action, *[str(a) for a in args]]
    proc = subprocess.run(cmd, cwd=PROJECT_DIR, capture_output=True, text=True, timeout=900)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or 'Worker failed').strip()
        raise RuntimeError(detail[-4000:])
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError('Worker returned no result')
    return json.loads(lines[-1])


async def worker_async(action: str, *args: object) -> dict:
    return await asyncio.to_thread(run_worker, action, *args)


async def regenerate_outputs() -> dict:
    return (await worker_async('generate'))['result']


async def refresh_all():
    async with refresh_lock:
        with connect() as conn:
            ids = [r['id'] for r in conn.execute('SELECT id FROM sources WHERE enabled=1 ORDER BY id')]
        results = []
        # Deliberately sequential: only one source/guide is resident in a short-lived worker at a time.
        for sid in ids:
            try:
                results.append((await worker_async('refresh', sid))['result'])
            except Exception as exc:
                results.append({'source_id': sid, 'status': 'error', 'error': str(exc)})
        output = await regenerate_outputs()
        return {'sources': results, 'output': output}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Generate in an isolated process so libxml allocations cannot inflate web-server idle RSS.
    try:
        await worker_async('reindex')
        await regenerate_outputs()
    except Exception:
        pass
    scheduler.add_job(
        refresh_all,
        CronTrigger(hour=f'*/{REFRESH_HOURS}', minute=0, timezone=ZoneInfo(TZ_NAME)),
        id='refresh-all', replace_existing=True, coalesce=True, max_instances=1,
    )
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title='IPTV Merge Manager', version='0.3.0', lifespan=lifespan)
app.mount('/static', StaticFiles(directory=APP_DIR / 'static'), name='static')
templates = Jinja2Templates(directory=APP_DIR / 'templates')


class ChannelPatch(BaseModel):
    selected: bool | None = None
    channel_number: int | None = None


class ChannelEdit(BaseModel):
    custom_name: str | None = None
    custom_group: str | None = None
    custom_tvg_id: str | None = None
    custom_logo: str | None = None
    channel_number: int | None = None


class BulkAction(BaseModel):
    ids: list[int]
    action: str
    value: str | int | None = None


class ReorderRequest(BaseModel):
    ids: list[int]


class AutoNumberRequest(BaseModel):
    start: int = 1
    increment: int = 1
    mode: str = 'sequential'


class GroupCreate(BaseModel):
    name: str
    number_start: int = 1
    number_increment: int = 1


class ProfilePatch(BaseModel):
    resource_profile: str


def _read_proc_kb(field: str) -> int | None:
    try:
        for line in Path('/proc/self/status').read_text().splitlines():
            if line.startswith(field + ':'):
                return int(line.split()[1])
    except Exception:
        pass
    return None


def _read_cgroup_bytes(name: str) -> int | None:
    candidates = [Path('/sys/fs/cgroup') / name]
    if name == 'memory.current':
        candidates.append(Path('/sys/fs/cgroup/memory/memory.usage_in_bytes'))
    elif name == 'memory.max':
        candidates.append(Path('/sys/fs/cgroup/memory/memory.limit_in_bytes'))
    for path in candidates:
        try:
            value = path.read_text().strip()
            return None if value == 'max' else int(value)
        except Exception:
            continue
    return None


def _tree_size(path: Path) -> int:
    total = 0
    if path.exists():
        for p in path.rglob('*'):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                pass
    return total


async def save_upload(upload: UploadFile, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('wb') as fh:
        while True:
            chunk = await upload.read(UPLOAD_CHUNK)
            if not chunk:
                break
            fh.write(chunk)
    await upload.close()


@app.get('/', response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse('index.html', {'request': request, 'refresh_hours': REFRESH_HOURS})


@app.get('/health')
def health():
    return {'status': 'ok', 'version': '0.3.0'}


@app.get('/api/status')
def status():
    profile, cfg = profile_config()
    with connect() as c:
        source_count = c.execute('SELECT COUNT(*) FROM sources').fetchone()[0]
        active = c.execute('SELECT COUNT(*) FROM channels WHERE is_active=1').fetchone()[0]
        selected = c.execute('SELECT COUNT(*) FROM channels WHERE selected=1 AND is_active=1').fetchone()[0]
        with_epg = c.execute(
            '''SELECT COUNT(*) FROM channels c WHERE c.selected=1 AND c.is_active=1
               AND COALESCE(NULLIF(c.custom_tvg_id,''),c.tvg_id) IS NOT NULL
               AND EXISTS(SELECT 1 FROM epg_channels e WHERE e.source_id=c.source_id
                          AND e.tvg_id=COALESCE(NULLIF(c.custom_tvg_id,''),c.tvg_id))'''
        ).fetchone()[0]
        missing = selected - with_epg
        groups = c.execute(
            "SELECT COUNT(DISTINCT COALESCE(NULLIF(custom_group,''),NULLIF(group_title,''))) FROM channels WHERE selected=1 AND is_active=1"
        ).fetchone()[0]
        logs = rows_to_dicts(c.execute(
            'SELECT l.*,s.name source_name FROM refresh_log l LEFT JOIN sources s ON s.id=l.source_id ORDER BY l.id DESC LIMIT 20'
        ).fetchall())
        unnumbered = c.execute('SELECT COUNT(*) FROM channels WHERE selected=1 AND is_active=1 AND channel_number IS NULL').fetchone()[0]
        last_peak = c.execute('SELECT peak_rss_kb FROM refresh_log WHERE peak_rss_kb IS NOT NULL ORDER BY id DESC LIMIT 1').fetchone()
    return {
        'version': '0.3.0', 'timezone': TZ_NAME, 'refresh_hours': REFRESH_HOURS,
        'source_count': source_count, 'active_channels': active, 'selected_channels': selected,
        'with_epg': with_epg, 'missing_epg': missing, 'group_count': groups, 'unnumbered': unnumbered,
        'logs': logs, 'resource_profile': profile, 'default_page_size': cfg['page_size'],
        'memory': {
            'web_rss_kb': _read_proc_kb('VmRSS'),
            'web_peak_kb': _read_proc_kb('VmHWM'),
            'container_current_bytes': _read_cgroup_bytes('memory.current'),
            'container_limit_bytes': _read_cgroup_bytes('memory.max'),
            'last_refresh_peak_kb': last_peak['peak_rss_kb'] if last_peak else None,
            'cache_bytes': _tree_size(CACHE_DIR),
            'output_bytes': _tree_size(OUTPUT_DIR),
        },
    }


@app.post('/api/settings/profile')
def set_profile(req: ProfilePatch):
    if req.resource_profile not in PROFILES:
        raise HTTPException(400, 'Unknown resource profile')
    cfg = PROFILES[req.resource_profile]
    set_setting('resource_profile', req.resource_profile)
    set_setting('history_limit', str(cfg['history_limit']))
    return {'ok': True, 'profile': req.resource_profile, **cfg}


@app.get('/api/sources')
def sources():
    with connect() as c:
        return rows_to_dicts(c.execute('SELECT * FROM sources ORDER BY id').fetchall())


@app.post('/api/sources')
async def add_source(
    name: str = Form(...), m3u_url: str | None = Form(None), xml_url: str | None = Form(None),
    m3u_file: UploadFile | None = File(None), xml_file: UploadFile | None = File(None),
):
    name = name.strip(); m3u_url = (m3u_url or '').strip() or None; xml_url = (xml_url or '').strip() or None
    if m3u_file and not m3u_file.filename: m3u_file = None
    if xml_file and not xml_file.filename: xml_file = None
    if not name: raise HTTPException(400, 'Source name is required')
    if not m3u_url and not m3u_file: raise HTTPException(400, 'Provide an M3U URL or file')
    if m3u_url and m3u_file: raise HTTPException(400, 'Use either M3U URL or upload')
    if xml_url and xml_file: raise HTTPException(400, 'Use either XMLTV URL or upload')
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    if m3u_file:
        path = UPLOAD_DIR / f'{uuid.uuid4().hex}{Path(m3u_file.filename).suffix or ".m3u"}'
        await save_upload(m3u_file, path); mk, mv = 'file', str(path)
    else:
        mk, mv = 'url', m3u_url
    xk = xv = None
    if xml_file:
        path = UPLOAD_DIR / f'{uuid.uuid4().hex}{"".join(Path(xml_file.filename).suffixes) or ".xml"}'
        await save_upload(xml_file, path); xk, xv = 'file', str(path)
    elif xml_url:
        xk, xv = 'url', xml_url
    with connect() as c:
        sid = c.execute('INSERT INTO sources(name,m3u_kind,m3u_value,xml_kind,xml_value) VALUES(?,?,?,?,?)', (name, mk, mv, xk, xv)).lastrowid
    try:
        async with refresh_lock:
            r = (await worker_async('refresh', sid))['result']
            await regenerate_outputs()
        return {'id': sid, 'refresh': r}
    except Exception as exc:
        return {'id': sid, 'refresh': {'status': 'error', 'error': str(exc)}}


@app.delete('/api/sources/{sid}')
async def delete_source(sid: int):
    with connect() as c:
        if not c.execute('SELECT 1 FROM sources WHERE id=?', (sid,)).fetchone(): raise HTTPException(404, 'Source not found')
        c.execute('DELETE FROM sources WHERE id=?', (sid,))
    await regenerate_outputs(); return {'ok': True}


@app.post('/api/sources/{sid}/refresh')
async def refresh_one(sid: int):
    async with refresh_lock:
        r = (await worker_async('refresh', sid))['result']
        return {'refresh': r, 'output': await regenerate_outputs()}


@app.post('/api/refresh')
async def refresh_everything():
    return await refresh_all()


@app.get('/api/channels')
def channels(
    source_id: int | None = None, group: str | None = None, q: str | None = None,
    selected: bool | None = None, active: bool = True, page: int = 1, page_size: int | None = None,
):
    _, cfg = profile_config()
    page = max(1, page)
    page_size = max(25, min(5000, page_size or cfg['page_size']))
    where = ['1=1']; params: list[object] = []
    if active: where.append('c.is_active=1')
    if source_id is not None: where.append('c.source_id=?'); params.append(source_id)
    if group: where.append("COALESCE(NULLIF(c.custom_group,''),c.group_title,'')=?"); params.append(group)
    if q:
        like = f'%{q}%'; where.append("(c.name LIKE ? OR COALESCE(c.custom_name,'') LIKE ? OR COALESCE(c.tvg_id,'') LIKE ? OR s.name LIKE ?)"); params += [like]*4
    if selected is not None: where.append('c.selected=?'); params.append(1 if selected else 0)
    base = f'''FROM channels c JOIN sources s ON s.id=c.source_id WHERE {' AND '.join(where)}'''
    with connect() as c:
        total = c.execute('SELECT COUNT(*) ' + base, params).fetchone()[0]
        sql = '''SELECT c.*,s.name source_name,
                 COALESCE(NULLIF(c.custom_name,''),c.name) display_name,
                 COALESCE(NULLIF(c.custom_group,''),c.group_title) display_group,
                 COALESCE(NULLIF(c.custom_tvg_id,''),c.tvg_id) display_tvg_id,
                 COALESCE(NULLIF(c.custom_logo,''),c.logo) display_logo ''' + base + \
              ' ORDER BY CASE WHEN c.selected=1 THEN 0 ELSE 1 END,c.sort_order,c.name COLLATE NOCASE LIMIT ? OFFSET ?'
        items = rows_to_dicts(c.execute(sql, [*params, page_size, (page-1)*page_size]).fetchall())
    return {'items': items, 'total': total, 'page': page, 'page_size': page_size, 'pages': max(1, (total + page_size - 1)//page_size)}


async def _regenerate_after_edit() -> None:
    await regenerate_outputs()


@app.patch('/api/channels/{cid}')
async def patch_channel(cid: int, patch: ChannelPatch):
    fields = []; params = []
    if patch.selected is not None: fields.append('selected=?'); params.append(1 if patch.selected else 0)
    if 'channel_number' in patch.model_fields_set: fields.append('channel_number=?'); params.append(patch.channel_number)
    if fields:
        with connect() as c: c.execute(f"UPDATE channels SET {','.join(fields)} WHERE id=?", [*params, cid])
        await _regenerate_after_edit()
    return {'ok': True}


@app.put('/api/channels/{cid}/edit')
async def edit_channel(cid: int, e: ChannelEdit):
    vals = [(x.strip() if isinstance(x, str) else x) or None for x in (e.custom_name, e.custom_group, e.custom_tvg_id, e.custom_logo)]
    with connect() as c:
        c.execute('UPDATE channels SET custom_name=?,custom_group=?,custom_tvg_id=?,custom_logo=?,channel_number=? WHERE id=?', (*vals, e.channel_number, cid))
    await _regenerate_after_edit(); return {'ok': True}


@app.post('/api/channels/bulk')
async def bulk(req: BulkAction):
    if not req.ids: return {'updated': 0}
    ph = ','.join('?' * len(req.ids)); field = None; value = req.value
    if req.action == 'enable': field = 'selected'; value = 1
    elif req.action == 'disable': field = 'selected'; value = 0
    elif req.action == 'group': field = 'custom_group'; value = str(value or '').strip() or None
    elif req.action == 'clear-numbers': field = 'channel_number'; value = None
    else: raise HTTPException(400, 'Unknown bulk action')
    with connect() as c: cur = c.execute(f'UPDATE channels SET {field}=? WHERE id IN ({ph})', [value, *req.ids])
    await _regenerate_after_edit(); return {'updated': cur.rowcount}


@app.post('/api/lineup/reorder')
async def reorder(req: ReorderRequest):
    with connect() as c:
        for i, cid in enumerate(req.ids, 1): c.execute('UPDATE channels SET sort_order=? WHERE id=? AND selected=1', (i*10, cid))
    await _regenerate_after_edit(); return {'ok': True}


@app.post('/api/lineup/autonumber')
async def autonumber(req: AutoNumberRequest):
    with connect() as c:
        rows = c.execute('SELECT id,channel_number FROM channels WHERE selected=1 AND is_active=1 ORDER BY sort_order,id').fetchall(); n = req.start
        if req.mode == 'fill-gaps':
            used = {r['channel_number'] for r in rows if r['channel_number'] is not None}
            for r in rows:
                if r['channel_number'] is not None: continue
                while n in used: n += req.increment
                c.execute('UPDATE channels SET channel_number=? WHERE id=?', (n, r['id'])); used.add(n); n += req.increment
        else:
            for r in rows: c.execute('UPDATE channels SET channel_number=? WHERE id=?', (n, r['id'])); n += req.increment
    await _regenerate_after_edit(); return {'ok': True}


@app.get('/api/groups')
def groups():
    with connect() as c:
        configured = rows_to_dicts(c.execute('SELECT * FROM lineup_groups ORDER BY sort_order,id').fetchall())
        names = [r[0] for r in c.execute("SELECT DISTINCT COALESCE(NULLIF(custom_group,''),group_title) g FROM channels WHERE is_active=1 AND g IS NOT NULL AND TRIM(g)<>'' ORDER BY g COLLATE NOCASE").fetchall()]
    return {'configured': configured, 'names': names}


@app.post('/api/groups')
def create_group(g: GroupCreate):
    with connect() as c:
        order = c.execute('SELECT COALESCE(MAX(sort_order),0)+10 FROM lineup_groups').fetchone()[0]
        c.execute('INSERT INTO lineup_groups(name,sort_order,number_start,number_increment) VALUES(?,?,?,?)', (g.name.strip(), order, g.number_start, g.number_increment))
    return {'ok': True}


@app.delete('/api/groups/{gid}')
def delete_group(gid: int):
    with connect() as c: c.execute('DELETE FROM lineup_groups WHERE id=?', (gid,))
    return {'ok': True}


@app.post('/api/groups/autonumber')
async def group_autonumber():
    with connect() as c:
        groups = c.execute('SELECT * FROM lineup_groups ORDER BY sort_order,id').fetchall()
        for g in groups:
            rows = c.execute("SELECT id FROM channels WHERE selected=1 AND is_active=1 AND COALESCE(NULLIF(custom_group,''),group_title)=? ORDER BY sort_order,id", (g['name'],)).fetchall(); n = g['number_start']
            for r in rows: c.execute('UPDATE channels SET channel_number=? WHERE id=?', (n, r['id'])); n += g['number_increment']
    await _regenerate_after_edit(); return {'ok': True}


@app.get('/api/channels/{cid}/epg-suggestions')
async def epg_suggestions(cid: int):
    with connect() as c:
        if not c.execute('SELECT 1 FROM channels WHERE id=?', (cid,)).fetchone(): raise HTTPException(404, 'Channel not found')
    # On-demand and isolated: the web process never parses XMLTV.
    return (await worker_async('epg-suggest', cid))['result']


@app.get('/api/backup')
def backup():
    out = OUTPUT_DIR / 'iptv-merge-manager-backup.zip'; OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        db_copy = Path(td) / 'iptv.db'
        with connect() as source:
            import sqlite3
            target = sqlite3.connect(db_copy)
            source.backup(target); target.close()
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
            z.write(db_copy, 'data/iptv.db')
            if UPLOAD_DIR.exists():
                for p in UPLOAD_DIR.rglob('*'):
                    if p.is_file(): z.write(p, 'data/uploads/' + p.name)
    return FileResponse(out, media_type='application/zip', filename='iptv-merge-manager-backup.zip')


@app.post('/api/restore')
async def restore(file: UploadFile = File(...)):
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td); zp = td_path / 'backup.zip'
        await save_upload(file, zp)
        try:
            with zipfile.ZipFile(zp) as z:
                names = set(z.namelist())
                if 'data/iptv.db' not in names: raise HTTPException(400, 'Backup does not contain data/iptv.db')
                if any(Path(n).is_absolute() or '..' in Path(n).parts for n in names): raise HTTPException(400, 'Unsafe backup ZIP')
                z.extractall(td_path)
            shutil.copy2(td_path / 'data/iptv.db', DB_PATH)
            up = td_path / 'data/uploads'
            if up.exists():
                UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                for p in up.iterdir():
                    if p.is_file(): shutil.copy2(p, UPLOAD_DIR / p.name)
        except zipfile.BadZipFile:
            raise HTTPException(400, 'Invalid backup ZIP')
    init_db(); await regenerate_outputs(); return {'ok': True}


@app.get('/output/master.m3u')
async def m3u():
    p = OUTPUT_DIR / 'master.m3u'
    if not p.exists(): await regenerate_outputs()
    return FileResponse(p, media_type='audio/x-mpegurl', filename='master.m3u')


@app.get('/output/master.xml')
async def xml():
    p = OUTPUT_DIR / 'master.xml'
    if not p.exists(): await regenerate_outputs()
    return FileResponse(p, media_type='application/xml', filename='master.xml')


@app.get('/output/master.xml.gz')
async def xml_gz():
    p = OUTPUT_DIR / 'master.xml.gz'
    if not p.exists(): await regenerate_outputs()
    return FileResponse(p, media_type='application/gzip', filename='master.xml.gz')
