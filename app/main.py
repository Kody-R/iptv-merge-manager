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

import httpx
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .db import DB_PATH, connect, get_setting, init_db, rows_to_dicts, set_setting
from .hls_proxy import (
    build_compat_master, build_locked_master, channel_diagnostics, effective_hls_height,
    effective_hls_mode, fetch_manifest, invalidate as invalidate_hls_cache, proxy_stats,
    record_bypass, record_event, record_failure, record_playlist_request, record_request,
    acquire_protected_segment, configure_protected, prepare_segment_relay, protected_segment_headers,
    protected_settings, record_variant_reresolve, resolve_master,
    resolve_playlist_token, resolve_segment_token, rewrite_media_playlist, segment_relay_headers,
    select_variant, stream_segment_relay, VALID_HLS_MODES,
)
from .stabilizer import (
    StabilizerSupervisor, VALID_STABILIZER_MODES, effective_stabilizer_mode, stabilizer_settings,
)

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
stabilizer_supervisor: StabilizerSupervisor | None = None

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
    global stabilizer_supervisor
    init_db()
    stabilizer_supervisor = StabilizerSupervisor()
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
    if stabilizer_supervisor:
        stabilizer_supervisor.shutdown()
        stabilizer_supervisor = None


app = FastAPI(title='IPTV Merge Manager', version='0.4.0', lifespan=lifespan)
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
    hls_proxy_enabled: bool | None = None  # retained for v0.3.1 API compatibility
    hls_mode: str | None = None
    hls_max_height: int | None = None
    stabilizer_mode: str | None = None


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


class HlsProxySettingsPatch(BaseModel):
    enabled: bool
    default_mode: str = 'direct'
    default_max_height: int = 720
    cache_seconds: int = 15
    protected_prefetch: int = 2
    protected_segment_timeout: float = 15.0
    protected_retries: int = 2
    protected_skip_failed: bool = True
    protected_cache_limit_mb: int = 512
    protected_cache_retention: int = 180


class SourceHlsPatch(BaseModel):
    mode: str = 'inherit'
    max_height: int | None = None


class SourceStabilizerPatch(BaseModel):
    mode: str = 'off'


class StabilizerSettingsPatch(BaseModel):
    enabled: bool = True
    default_mode: str = 'off'
    idle_timeout_seconds: int = 60
    stall_timeout_seconds: int = 8
    startup_timeout_seconds: int = 15
    ready_segments: int = 2
    hls_time: int = 3
    hls_list_size: int = 12
    hls_delete_threshold: int = 4
    dts_delta_threshold: float = 1.0
    auto_restart: bool = True
    max_workers: int = 12
    x264_preset: str = 'veryfast'
    x264_crf: int = 20
    audio_bitrate: str = '160k'


def _sync_protected_runtime() -> dict:
    return configure_protected(
        prefetch_depth=int(get_setting('hls_protected_prefetch', '2')),
        segment_timeout=float(get_setting('hls_protected_segment_timeout', '15')),
        retries=int(get_setting('hls_protected_retries', '2')),
        skip_failed=get_setting('hls_protected_skip_failed', '1') == '1',
        cache_limit_mb=int(get_setting('hls_protected_cache_limit_mb', '512')),
        retention_seconds=int(get_setting('hls_protected_cache_retention', '180')),
    )


def _stabilizer() -> StabilizerSupervisor:
    if stabilizer_supervisor is None:
        raise HTTPException(503, 'Stabilizer supervisor is not ready')
    return stabilizer_supervisor


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
    return {'status': 'ok', 'version': '0.4.0'}


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
        stab_rows = c.execute(
            '''SELECT c.stabilizer_mode,s.stabilizer_mode source_stabilizer_mode
               FROM channels c JOIN sources s ON s.id=c.source_id
               WHERE c.is_active=1 AND s.enabled=1'''
        ).fetchall()
    stab_cfg = stabilizer_settings()
    configured_stabilized = sum(
        1 for r in stab_rows
        if effective_stabilizer_mode(r['stabilizer_mode'], r['source_stabilizer_mode'], stab_cfg['default_mode']) != 'off'
    )
    stab_runtime = stabilizer_supervisor.summary() if stabilizer_supervisor else {
        'active_workers': 0, 'known_workers': 0, 'restarts': 0, 'stalls': 0, 'errors': 0, 'workers': []
    }
    return {
        'version': '0.4.0', 'timezone': TZ_NAME, 'refresh_hours': REFRESH_HOURS,
        'source_count': source_count, 'active_channels': active, 'selected_channels': selected,
        'with_epg': with_epg, 'missing_epg': missing, 'group_count': groups, 'unnumbered': unnumbered,
        'logs': logs, 'resource_profile': profile, 'default_page_size': cfg['page_size'],
        'hls_proxy': {
            'enabled': get_setting('hls_proxy_enabled', '1') == '1',
            'default_mode': get_setting('hls_proxy_default_mode', 'direct'),
            'default_max_height': int(get_setting('hls_proxy_default_height', '720')),
            'cache_seconds': int(get_setting('hls_proxy_cache_seconds', '15')),
            'protected': _sync_protected_runtime(),
            **proxy_stats(),
        },
        'stabilizer': {
            **stab_cfg,
            'configured_channels': configured_stabilized,
            **stab_runtime,
        },
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


@app.post('/api/settings/hls-proxy')
async def set_hls_proxy_settings(req: HlsProxySettingsPatch):
    mode = (req.default_mode or 'direct').strip().lower()
    if mode not in VALID_HLS_MODES:
        raise HTTPException(400, 'Unknown HLS mode')
    height = max(0, min(4320, int(req.default_max_height)))
    cache_seconds = max(1, min(300, int(req.cache_seconds)))
    protected_prefetch = max(0, min(8, int(req.protected_prefetch)))
    protected_segment_timeout = max(5.0, min(60.0, float(req.protected_segment_timeout)))
    protected_retries = max(1, min(4, int(req.protected_retries)))
    protected_cache_limit_mb = max(64, min(4096, int(req.protected_cache_limit_mb)))
    protected_cache_retention = max(30, min(3600, int(req.protected_cache_retention)))
    set_setting('hls_proxy_enabled', '1' if req.enabled else '0')
    set_setting('hls_proxy_default_mode', mode)
    set_setting('hls_proxy_default_height', str(height))
    set_setting('hls_proxy_cache_seconds', str(cache_seconds))
    set_setting('hls_protected_prefetch', str(protected_prefetch))
    set_setting('hls_protected_segment_timeout', str(protected_segment_timeout))
    set_setting('hls_protected_retries', str(protected_retries))
    set_setting('hls_protected_skip_failed', '1' if req.protected_skip_failed else '0')
    set_setting('hls_protected_cache_limit_mb', str(protected_cache_limit_mb))
    set_setting('hls_protected_cache_retention', str(protected_cache_retention))
    protected = _sync_protected_runtime()
    await regenerate_outputs()
    return {'ok': True, 'enabled': req.enabled, 'default_mode': mode, 'default_max_height': height, 'cache_seconds': cache_seconds, 'protected': protected}


@app.post('/api/settings/stabilizer')
async def set_stabilizer_settings(req: StabilizerSettingsPatch):
    mode = (req.default_mode or 'off').strip().lower()
    if mode not in VALID_STABILIZER_MODES:
        raise HTTPException(400, 'Unknown stabilizer mode')
    preset = (req.x264_preset or 'veryfast').strip().lower()
    if preset not in {'ultrafast','superfast','veryfast','faster','fast','medium','slow'}:
        raise HTTPException(400, 'Unsupported x264 preset')
    audio_bitrate = (req.audio_bitrate or '160k').strip().lower()
    if not audio_bitrate.endswith('k') or not audio_bitrate[:-1].isdigit():
        raise HTTPException(400, 'Audio bitrate must look like 128k or 160k')
    values = {
        'stabilizer_enabled': '1' if req.enabled else '0',
        'stabilizer_default_mode': mode,
        'stabilizer_idle_timeout': str(max(15, min(3600, int(req.idle_timeout_seconds)))),
        'stabilizer_stall_timeout': str(max(4, min(300, int(req.stall_timeout_seconds)))),
        'stabilizer_startup_timeout': str(max(5, min(120, int(req.startup_timeout_seconds)))),
        'stabilizer_ready_segments': str(max(1, min(6, int(req.ready_segments)))),
        'stabilizer_hls_time': str(max(1, min(10, int(req.hls_time)))),
        'stabilizer_hls_list_size': str(max(4, min(60, int(req.hls_list_size)))),
        'stabilizer_hls_delete_threshold': str(max(1, min(20, int(req.hls_delete_threshold)))),
        'stabilizer_dts_delta_threshold': str(max(0.1, min(30.0, float(req.dts_delta_threshold)))),
        'stabilizer_auto_restart': '1' if req.auto_restart else '0',
        'stabilizer_max_workers': str(max(1, min(64, int(req.max_workers)))),
        'stabilizer_x264_preset': preset,
        'stabilizer_x264_crf': str(max(14, min(32, int(req.x264_crf)))),
        'stabilizer_audio_bitrate': audio_bitrate,
    }
    for key, value in values.items():
        set_setting(key, value)
    await regenerate_outputs()
    return {'ok': True, **stabilizer_settings()}


@app.get('/api/sources')
def sources():
    with connect() as c:
        return rows_to_dicts(c.execute('SELECT * FROM sources ORDER BY id').fetchall())


@app.patch('/api/sources/{sid}/hls')
async def set_source_hls(sid: int, req: SourceHlsPatch):
    mode = (req.mode or 'inherit').strip().lower()
    if mode not in {'inherit', *VALID_HLS_MODES}:
        raise HTTPException(400, 'Unknown source HLS mode')
    height = None if req.max_height is None else max(0, min(4320, int(req.max_height)))
    with connect() as c:
        if not c.execute('SELECT 1 FROM sources WHERE id=?', (sid,)).fetchone():
            raise HTTPException(404, 'Source not found')
        c.execute('UPDATE sources SET hls_mode=?,hls_max_height=? WHERE id=?', (mode, height, sid))
    await regenerate_outputs()
    return {'ok': True, 'mode': mode, 'max_height': height}


@app.patch('/api/sources/{sid}/stabilizer')
async def set_source_stabilizer(sid: int, req: SourceStabilizerPatch):
    mode = (req.mode or 'off').strip().lower()
    if mode not in {'inherit', *VALID_STABILIZER_MODES}:
        raise HTTPException(400, 'Unknown source stabilizer mode')
    with connect() as c:
        if not c.execute('SELECT 1 FROM sources WHERE id=?', (sid,)).fetchone():
            raise HTTPException(404, 'Source not found')
        c.execute('UPDATE sources SET stabilizer_mode=? WHERE id=?', (mode, sid))
    await regenerate_outputs()
    return {'ok': True, 'mode': mode}


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
        sql = '''SELECT c.*,s.name source_name,s.hls_mode source_hls_mode,s.hls_max_height source_hls_max_height,
                 s.stabilizer_mode source_stabilizer_mode,
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
        old = c.execute('SELECT hls_mode,hls_proxy_enabled,hls_max_height,stabilizer_mode FROM channels WHERE id=?', (cid,)).fetchone()
        if not old:
            raise HTTPException(404, 'Channel not found')
        mode = old['hls_mode']
        if 'hls_mode' in e.model_fields_set:
            mode = (e.hls_mode or '').strip().lower() or None
        elif 'hls_proxy_enabled' in e.model_fields_set and e.hls_proxy_enabled is not None:
            mode = 'fixed' if e.hls_proxy_enabled else 'direct'
        if mode not in ({None} | VALID_HLS_MODES):
            raise HTTPException(400, 'Unknown channel HLS mode')
        proxy_enabled = 1 if mode in {'compat', 'fixed', 'protected'} else 0
        max_height = old['hls_max_height']
        if 'hls_max_height' in e.model_fields_set:
            max_height = e.hls_max_height if e.hls_max_height is not None and e.hls_max_height >= 0 else None
        stabilizer_mode = old['stabilizer_mode']
        if 'stabilizer_mode' in e.model_fields_set:
            stabilizer_mode = (e.stabilizer_mode or '').strip().lower() or None
        if stabilizer_mode not in ({None} | VALID_STABILIZER_MODES):
            raise HTTPException(400, 'Unknown channel stabilizer mode')
        c.execute(
            'UPDATE channels SET custom_name=?,custom_group=?,custom_tvg_id=?,custom_logo=?,channel_number=?,hls_proxy_enabled=?,hls_mode=?,hls_max_height=?,stabilizer_mode=? WHERE id=?',
            (*vals, e.channel_number, proxy_enabled, mode, max_height, stabilizer_mode, cid),
        )
    await _regenerate_after_edit(); return {'ok': True}


@app.post('/api/channels/bulk')
async def bulk(req: BulkAction):
    if not req.ids: return {'updated': 0}
    ph = ','.join('?' * len(req.ids)); field = None; value = req.value
    if req.action == 'enable': field = 'selected'; value = 1
    elif req.action == 'disable': field = 'selected'; value = 0
    elif req.action == 'group': field = 'custom_group'; value = str(value or '').strip() or None
    elif req.action == 'clear-numbers': field = 'channel_number'; value = None
    elif req.action == 'hls-proxy-on': field = 'hls_mode'; value = 'fixed'
    elif req.action == 'hls-proxy-off': field = 'hls_mode'; value = 'direct'
    elif req.action == 'hls-mode-fixed': field = 'hls_mode'; value = 'fixed'
    elif req.action == 'hls-mode-protected': field = 'hls_mode'; value = 'protected'
    elif req.action == 'hls-mode-compat': field = 'hls_mode'; value = 'compat'
    elif req.action == 'hls-mode-direct': field = 'hls_mode'; value = 'direct'
    elif req.action == 'hls-mode-inherit': field = 'hls_mode'; value = None
    elif req.action == 'hls-720': field = 'hls_max_height'; value = 720
    elif req.action == 'hls-540': field = 'hls_max_height'; value = 540
    elif req.action == 'hls-360': field = 'hls_max_height'; value = 360
    elif req.action == 'hls-default': field = 'hls_max_height'; value = None
    elif req.action == 'stabilizer-inherit': field = 'stabilizer_mode'; value = None
    elif req.action == 'stabilizer-off': field = 'stabilizer_mode'; value = 'off'
    elif req.action == 'stabilizer-remux': field = 'stabilizer_mode'; value = 'remux'
    elif req.action == 'stabilizer-transcode': field = 'stabilizer_mode'; value = 'transcode'
    else: raise HTTPException(400, 'Unknown bulk action')
    with connect() as c:
        cur = c.execute(f'UPDATE channels SET {field}=? WHERE id IN ({ph})', [value, *req.ids])
        if field == 'hls_mode':
            legacy_enabled = 1 if value in {'compat', 'fixed', 'protected'} else 0
            c.execute(f'UPDATE channels SET hls_proxy_enabled=? WHERE id IN ({ph})', [legacy_enabled, *req.ids])
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
async def m3u(request: Request):
    p = OUTPUT_DIR / 'master.m3u'
    if not p.exists():
        await regenerate_outputs()
    base = str(request.base_url).rstrip('/')

    def stream_playlist():
        with p.open('r', encoding='utf-8', errors='replace') as fh:
            for line in fh:
                yield line.replace('__IPTVMM_BASE__', base)

    return StreamingResponse(
        stream_playlist(),
        media_type='audio/x-mpegurl',
        headers={'Content-Disposition': 'inline; filename=master.m3u', 'Cache-Control': 'no-cache'},
    )


@app.get('/api/channels/{cid}/stabilizer-diagnostics')
def stabilizer_diagnostics(cid: int):
    with connect() as c:
        if not c.execute('SELECT 1 FROM channels WHERE id=?', (cid,)).fetchone():
            raise HTTPException(404, 'Channel not found')
    return {'settings': stabilizer_settings(), 'runtime': _stabilizer().status_for(cid)}


@app.post('/api/channels/{cid}/stabilizer/restart')
def restart_stabilizer(cid: int):
    try:
        rt = _stabilizer().restart(cid, reason='manual')
    except KeyError:
        raise HTTPException(404, 'Channel not found')
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    return {'ok': True, 'runtime': rt.status()}


@app.post('/api/channels/{cid}/stabilizer/stop')
def stop_stabilizer(cid: int):
    _stabilizer().stop(cid, reason='manual')
    return {'ok': True}


@app.get('/stabilized/channel/{cid}/index.m3u8')
async def stabilized_playlist(cid: int):
    try:
        path = await _stabilizer().ensure_ready(cid)
    except KeyError:
        raise HTTPException(404, 'Channel not found')
    except TimeoutError as exc:
        raise HTTPException(503, str(exc), headers={'Retry-After': '2'})
    except RuntimeError as exc:
        raise HTTPException(502, str(exc))
    supervisor = _stabilizer()
    supervisor.touch(cid)
    runtime = supervisor.status_for(cid)
    return Response(
        path.read_text(encoding='utf-8', errors='replace'),
        media_type='application/vnd.apple.mpegurl',
        headers={
            'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
            'Pragma': 'no-cache',
            'X-IPTVMM-Stabilizer': str(runtime.get('effective_mode') or runtime.get('mode') or 'enabled'),
            'X-IPTVMM-Acquisition': str(runtime.get('acquisition_mode') or 'direct'),
        },
    )


@app.get('/stabilized/channel/{cid}/{filename}')
def stabilized_segment(cid: int, filename: str):
    if '/' in filename or '\\' in filename or '..' in filename or not filename.endswith(('.ts', '.m4s', '.mp4', '.aac')):
        raise HTTPException(400, 'Invalid stabilized segment filename')
    supervisor = _stabilizer()
    path = supervisor.output_dir(cid) / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(404, 'Stabilized segment not found')
    supervisor.touch(cid)
    return FileResponse(path, media_type='video/mp2t', headers={'Cache-Control': 'private, max-age=20'})


@app.get('/api/channels/{cid}/hls-analyze')
async def hls_analyze(cid: int):
    with connect() as c:
        row = c.execute(
            '''SELECT c.id,c.name,c.stream_url,c.hls_mode,c.hls_max_height,
                      s.hls_mode source_hls_mode,s.hls_max_height source_hls_max_height
               FROM channels c JOIN sources s ON s.id=c.source_id WHERE c.id=?''',
            (cid,),
        ).fetchone()
    if not row:
        raise HTTPException(404, 'Channel not found')
    if not str(row['stream_url']).startswith(('http://', 'https://')):
        return {'adaptive': False, 'reason': 'Stream is not HTTP/HTTPS', 'variants': []}
    mode = effective_hls_mode(row['hls_mode'], row['source_hls_mode'], get_setting('hls_proxy_default_mode', 'direct'))
    height = effective_hls_height(row['hls_max_height'], row['source_hls_max_height'], int(get_setting('hls_proxy_default_height', '720')))
    try:
        cache_seconds = int(get_setting('hls_proxy_cache_seconds', '15'))
        entry = await resolve_master(row['stream_url'], cache_seconds)
        variants = [v.as_dict() for v in entry.variants]
        if not entry.variants:
            return {'adaptive': False, 'final_url': entry.final_url, 'variants': [], 'mode': mode, 'max_height': height}
        chosen = select_variant(entry.variants, height)
        return {
            'adaptive': True,
            'mode': mode,
            'max_height': height,
            'variants': variants,
            'selected': chosen.as_dict(),
        }
    except Exception as exc:
        raise HTTPException(502, f'HLS analysis failed: {exc}')


@app.get('/api/channels/{cid}/hls-diagnostics')
def hls_diagnostics(cid: int):
    with connect() as c:
        row = c.execute(
            '''SELECT c.id,c.name,c.hls_mode,c.hls_max_height,
                      s.hls_mode source_hls_mode,s.hls_max_height source_hls_max_height
               FROM channels c JOIN sources s ON s.id=c.source_id WHERE c.id=?''',
            (cid,),
        ).fetchone()
    if not row:
        raise HTTPException(404, 'Channel not found')
    mode = effective_hls_mode(row['hls_mode'], row['source_hls_mode'], get_setting('hls_proxy_default_mode', 'direct'))
    height = effective_hls_height(row['hls_max_height'], row['source_hls_max_height'], int(get_setting('hls_proxy_default_height', '720')))
    return {**channel_diagnostics(cid), 'name': row['name'], 'mode': mode, 'max_height': height}


def _hls_response(body: str, mode: str, height: int | None = None) -> Response:
    headers = {
        'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
        'Pragma': 'no-cache',
        'X-IPTVMM-HLS-Mode': mode,
    }
    if height is not None:
        headers['X-IPTVMM-Variant-Lock'] = str(height)
    return Response(content=body, media_type='application/vnd.apple.mpegurl', headers=headers)


@app.get('/hls/channel/{cid}/index.m3u8')
async def hls_proxy(cid: int, request: Request):
    record_request(cid)
    with connect() as c:
        row = c.execute(
            '''SELECT c.id,c.name,c.stream_url,c.hls_mode,c.hls_max_height,
                      s.hls_mode source_hls_mode,s.hls_max_height source_hls_max_height
               FROM channels c JOIN sources s ON s.id=c.source_id
               WHERE c.id=? AND c.is_active=1 AND s.enabled=1''',
            (cid,),
        ).fetchone()
    if not row:
        raise HTTPException(404, 'Channel not found')

    upstream = row['stream_url']
    if get_setting('hls_proxy_enabled', '1') != '1':
        record_bypass(cid)
        return RedirectResponse(upstream, status_code=307)

    mode = effective_hls_mode(row['hls_mode'], row['source_hls_mode'], get_setting('hls_proxy_default_mode', 'direct'))
    height = effective_hls_height(row['hls_max_height'], row['source_hls_max_height'], int(get_setting('hls_proxy_default_height', '720')))
    cache_seconds = int(get_setting('hls_proxy_cache_seconds', '15'))
    local_base = str(request.base_url).rstrip('/')
    if mode == 'protected':
        _sync_protected_runtime()

    if mode == 'direct':
        record_bypass(cid)
        return RedirectResponse(upstream, status_code=307)

    try:
        if mode == 'compat':
            master = await resolve_master(upstream, cache_seconds)
            if not master.manifest.startswith('#EXTM3U'):
                record_bypass(cid)
                return RedirectResponse(master.final_url, status_code=307)
            if master.variants:
                body = build_compat_master(master.manifest, master.final_url, cid, local_base)
            else:
                body = rewrite_media_playlist(master.manifest, master.final_url, cid, local_base, True, 'media')
            return _hls_response(body, mode)

        async def resolve_selected(force: bool = False):
            if force:
                await invalidate_hls_cache(upstream)
                record_variant_reresolve(cid)
            master = await resolve_master(upstream, cache_seconds)
            if not master.variants:
                return master, None
            return master, select_variant(master.variants, height)

        master, selected = await resolve_selected()
        if selected is None:
            if master.manifest.startswith('#EXTM3U'):
                body = rewrite_media_playlist(master.manifest, master.final_url, cid, local_base, True, 'media', relay_all=(mode == 'protected'))
                return _hls_response(body, mode, height)
            record_bypass(cid)
            return RedirectResponse(master.final_url, status_code=307)

        if selected.attrs.get('AUDIO'):
            body = build_locked_master(master.manifest, master.final_url, selected, cid, local_base, mode)
        else:
            try:
                media, media_url = await fetch_manifest(selected.absolute_uri)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in {401, 403, 404, 410}:
                    raise
                master, selected = await resolve_selected(force=True)
                if selected is None:
                    record_bypass(cid)
                    return RedirectResponse(master.final_url, status_code=307)
                media, media_url = await fetch_manifest(selected.absolute_uri)
            if not media.startswith('#EXTM3U'):
                body = build_locked_master(master.manifest, master.final_url, selected, cid, local_base, mode)
            else:
                body = rewrite_media_playlist(media, media_url, cid, local_base, True, 'media', relay_all=(mode == 'protected'))
        return _hls_response(body, mode, height)
    except Exception as exc:
        record_failure(cid, str(exc))
        raise HTTPException(502, f'HLS proxy failed: {exc}')


@app.get('/hls/channel/{cid}/playlist/{token}.m3u8')
async def hls_child_playlist(cid: int, token: str, request: Request):
    record_playlist_request(cid)
    entry = resolve_playlist_token(cid, token)
    if not entry:
        raise HTTPException(410, 'HLS child playlist token expired; reopen the channel to refresh it')
    local_base = str(request.base_url).rstrip('/')
    try:
        body, final_url = await fetch_manifest(entry.url)
        if not body.startswith('#EXTM3U'):
            raise HTTPException(502, 'Upstream child playlist did not return M3U8 data')
        from .hls_proxy import parse_master
        variants = parse_master(body, final_url)
        if variants:
            rewritten = build_compat_master(body, final_url, cid, local_base, entry.mode)
        else:
            rewritten = rewrite_media_playlist(body, final_url, cid, local_base, True, entry.kind, relay_all=(entry.mode == 'protected'))
        return _hls_response(rewritten, entry.mode)
    except HTTPException:
        raise
    except Exception as exc:
        record_failure(cid, str(exc))
        raise HTTPException(502, f'HLS child playlist failed: {exc}')


@app.get('/hls/channel/{cid}/segment/{token}.{suffix}')
async def hls_segment_relay(cid: int, token: str, suffix: str, request: Request):
    entry = resolve_segment_token(cid, token)
    if not entry:
        raise HTTPException(410, 'HLS segment alias expired')
    if entry.suffix.lower() != f'.{suffix.lower()}':
        raise HTTPException(404, 'HLS segment alias extension mismatch')

    if entry.protected:
        cfg = _sync_protected_runtime()
        try:
            result = await acquire_protected_segment(cid, token, entry)
        except Exception as exc:
            record_failure(cid, f'Protected atomic segment acquisition failed: {exc}')
            status = 410 if cfg.get('skip_failed', True) else 504
            raise HTTPException(status, f'Protected HLS segment could not be acquired within its bounded deadline: {exc}')
        return FileResponse(
            result.path,
            media_type=result.content_type or 'application/octet-stream',
            headers=protected_segment_headers(result),
        )

    try:
        relay = await prepare_segment_relay(cid, entry)
    except Exception as exc:
        record_failure(cid, f'Guarded segment relay failed: {exc}')
        raise HTTPException(504, f'HLS compatibility segment timed out or failed: {exc}')

    return StreamingResponse(
        stream_segment_relay(cid, relay, request.is_disconnected),
        media_type=relay.content_type or 'video/mp2t',
        headers=segment_relay_headers(relay),
    )


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
