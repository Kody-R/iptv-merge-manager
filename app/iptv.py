from __future__ import annotations

import gc
import gzip
import hashlib
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import httpx
from lxml import etree

from .db import connect, get_setting

DATA_DIR = Path(os.getenv('IPTVMM_DATA_DIR', '/app/data'))
CACHE_DIR = DATA_DIR / 'cache'
OUTPUT_DIR = Path(os.getenv('IPTVMM_OUTPUT_DIR', '/app/output'))
ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')
CHUNK_SIZE = 256 * 1024


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_name(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', (value or '').lower()).strip()


def stable_key(tvg_id: str | None, name: str, group_title: str | None, stream_url: str) -> str:
    if tvg_id and tvg_id.strip():
        return 'tvg:' + tvg_id.strip().lower()
    base = f"{normalize_name(name)}|{normalize_name(group_title or '')}"
    if normalize_name(name):
        return 'name:' + hashlib.sha1(base.encode()).hexdigest()
    return 'url:' + hashlib.sha1(stream_url.encode()).hexdigest()


def iter_m3u(path: Path) -> Iterator[dict]:
    """Parse an M3U one line at a time; never hold the source file or channel list in RAM."""
    used_keys: set[str] = set()
    pending: dict | None = None
    pending_group: str | None = None
    with path.open('r', encoding='utf-8', errors='replace', newline=None) as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            if line.startswith('#EXTINF'):
                attrs = {k.lower(): v for k, v in ATTR_RE.findall(line)}
                name = line.split(',', 1)[1].strip() if ',' in line else attrs.get('tvg-name', 'Unnamed Channel')
                pending = {
                    'tvg_id': attrs.get('tvg-id') or None,
                    'name': attrs.get('tvg-name') or name or 'Unnamed Channel',
                    'group_title': attrs.get('group-title') or None,
                    'logo': attrs.get('tvg-logo') or None,
                }
                pending_group = None
            elif line.startswith('#EXTGRP:') and pending is not None:
                pending_group = line.split(':', 1)[1].strip() or None
            elif line.startswith('#'):
                continue
            elif pending is not None:
                pending['stream_url'] = line
                if not pending.get('group_title') and pending_group:
                    pending['group_title'] = pending_group
                key = stable_key(pending.get('tvg_id'), pending['name'], pending.get('group_title'), line)
                if key in used_keys:
                    key += '|dup:' + hashlib.sha1(line.encode()).hexdigest()[:12]
                used_keys.add(key)
                pending['stable_key'] = key
                yield pending
                pending = None
                pending_group = None


async def _download_to_file(url: str, destination: Path) -> None:
    headers = {'User-Agent': 'IPTV-Merge-Manager/0.5.0'}
    timeout = httpx.Timeout(90.0, connect=20.0)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + '.download')
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, headers=headers) as client:
        async with client.stream('GET', url) as response:
            response.raise_for_status()
            with tmp.open('wb') as fh:
                async for chunk in response.aiter_bytes(CHUNK_SIZE):
                    fh.write(chunk)
    os.replace(tmp, destination)


def _copy_file_streaming(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + '.copy')
    with source.open('rb') as src, tmp.open('wb') as dst:
        shutil.copyfileobj(src, dst, length=CHUNK_SIZE)
    os.replace(tmp, destination)


def _decompress_gzip_file(source: Path, destination: Path) -> None:
    tmp = destination.with_suffix(destination.suffix + '.unpack')
    with gzip.open(source, 'rb') as src, tmp.open('wb') as dst:
        shutil.copyfileobj(src, dst, length=CHUNK_SIZE)
    os.replace(tmp, destination)


async def materialize_source(kind: str, value: str, destination: Path, is_xml: bool = False) -> None:
    """Materialize sources through disk, never response.content/read_bytes()."""
    raw = destination.with_suffix(destination.suffix + '.raw') if is_xml else destination
    if kind == 'url':
        await _download_to_file(value, raw)
    else:
        src = Path(value)
        if not src.exists():
            raise FileNotFoundError(f'Uploaded source file no longer exists: {src}')
        _copy_file_streaming(src, raw)

    if is_xml:
        with raw.open('rb') as fh:
            magic = fh.read(2)
        if magic == b'\x1f\x8b':
            _decompress_gzip_file(raw, destination)
            raw.unlink(missing_ok=True)
        elif raw != destination:
            os.replace(raw, destination)


def _clear_element(elem: etree._Element) -> None:
    elem.clear()
    parent = elem.getparent()
    if parent is not None:
        while elem.getprevious() is not None:
            del parent[0]




def count_xml_channels(xml_path: Path) -> int:
    count = 0
    context = etree.iterparse(str(xml_path), events=('end',), tag='channel', recover=False, huge_tree=True)
    for _, elem in context:
        count += 1
        _clear_element(elem)
    del context
    gc.collect()
    return count

def rebuild_epg_index(source_id: int, xml_path: Path) -> int:
    """Persist only channel IDs/display names in SQLite; programme data remains disk-backed."""
    with connect() as conn:
        conn.execute('DELETE FROM epg_channels WHERE source_id=?', (source_id,))
        if not xml_path.exists():
            return 0
        batch: list[tuple[int, str, str | None]] = []
        count = 0
        context = etree.iterparse(str(xml_path), events=('end',), tag='channel', recover=True, huge_tree=True)
        for _, elem in context:
            tvg_id = elem.get('id')
            if tvg_id:
                display = None
                child = elem.find('display-name')
                if child is not None:
                    display = ''.join(child.itertext()).strip() or None
                batch.append((source_id, tvg_id, display))
                count += 1
                if len(batch) >= 500:
                    conn.executemany('INSERT OR REPLACE INTO epg_channels(source_id,tvg_id,display_name) VALUES(?,?,?)', batch)
                    batch.clear()
            _clear_element(elem)
        if batch:
            conn.executemany('INSERT OR REPLACE INTO epg_channels(source_id,tvg_id,display_name) VALUES(?,?,?)', batch)
        del context, batch
    gc.collect()
    return count


def _trim_history() -> None:
    try:
        limit = max(1, int(get_setting('history_limit', '10')))
    except ValueError:
        limit = 10
    with connect() as conn:
        conn.execute(
            'DELETE FROM refresh_log WHERE id NOT IN (SELECT id FROM refresh_log ORDER BY id DESC LIMIT ?)',
            (limit,),
        )


async def refresh_source(source_id: int) -> dict:
    with connect() as conn:
        source = conn.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone()
        if not source:
            raise ValueError('Source not found')
        source = dict(source)
        log_id = conn.execute(
            'INSERT INTO refresh_log(source_id,started_at,status,message) VALUES(?,?,?,?)',
            (source_id, utc_now(), 'running', 'Refresh started'),
        ).lastrowid

    if not source['enabled']:
        return {'source_id': source_id, 'status': 'disabled'}

    m3u_cache = CACHE_DIR / f'source_{source_id}.m3u'
    xml_cache = CACHE_DIR / f'source_{source_id}.xml'
    m3u_candidate = CACHE_DIR / f'.source_{source_id}.m3u.candidate'
    xml_candidate = CACHE_DIR / f'.source_{source_id}.xml.candidate'
    try:
        # Stage to candidate files; last-known-good caches remain untouched until validation succeeds.
        await materialize_source(source['m3u_kind'], source['m3u_value'], m3u_candidate)
        count = sum(1 for _ in iter_m3u(m3u_candidate))
        if count == 0:
            raise ValueError('Rejected refresh: playlist contains no IPTV #EXTINF channels')
        previous_count = int(source.get('channel_count') or 0)
        if previous_count >= 20 and count < previous_count * 0.50:
            raise ValueError(f'Rejected refresh: sudden channel loss ({previous_count} -> {count}); last-known-good cache preserved')

        epg_count = None
        if source.get('xml_kind') and source.get('xml_value'):
            await materialize_source(source['xml_kind'], source['xml_value'], xml_candidate, is_xml=True)
            epg_count = count_xml_channels(xml_candidate)
            if xml_cache.exists():
                with connect() as conn:
                    previous_epg = conn.execute('SELECT COUNT(*) FROM epg_channels WHERE source_id=?', (source_id,)).fetchone()[0]
                if previous_epg >= 20 and epg_count < previous_epg * 0.50:
                    raise ValueError(f'Rejected refresh: sudden XMLTV channel loss ({previous_epg} -> {epg_count}); last-known-good cache preserved')

        now = utc_now()
        new_count = changed_count = 0
        with connect() as conn:
            conn.execute('CREATE TEMP TABLE seen_keys(stable_key TEXT PRIMARY KEY) WITHOUT ROWID')
            max_order = conn.execute('SELECT COALESCE(MAX(sort_order),0) FROM channels').fetchone()[0]
            for item in iter_m3u(m3u_candidate):
                key = item['stable_key']
                conn.execute('INSERT OR IGNORE INTO seen_keys(stable_key) VALUES(?)', (key,))
                old = conn.execute(
                    'SELECT id,tvg_id,name,group_title,logo,stream_url FROM channels WHERE source_id=? AND stable_key=?',
                    (source_id, key),
                ).fetchone()
                if old:
                    changed = any((old[field] or '') != (item.get(field) or '') for field in ('tvg_id','name','group_title','logo','stream_url'))
                    changed_count += int(changed)
                    conn.execute(
                        'UPDATE channels SET tvg_id=?,name=?,group_title=?,logo=?,stream_url=?,is_active=1,last_seen=? WHERE id=?',
                        (item.get('tvg_id'), item['name'], item.get('group_title'), item.get('logo'), item['stream_url'], now, old['id']),
                    )
                else:
                    max_order += 10
                    new_count += 1
                    conn.execute(
                        '''INSERT INTO channels(source_id,stable_key,tvg_id,name,group_title,logo,stream_url,selected,sort_order,channel_number,is_active,last_seen)
                           VALUES(?,?,?,?,?,?,?,0,?,NULL,1,?)''',
                        (source_id,key,item.get('tvg_id'),item['name'],item.get('group_title'),item.get('logo'),item['stream_url'],max_order,now),
                    )
            removed_count = conn.execute(
                '''SELECT COUNT(*) FROM channels WHERE source_id=? AND is_active=1
                   AND stable_key NOT IN (SELECT stable_key FROM seen_keys)''', (source_id,)
            ).fetchone()[0]
            conn.execute(
                '''UPDATE channels SET is_active=0 WHERE source_id=? AND is_active=1
                   AND stable_key NOT IN (SELECT stable_key FROM seen_keys)''', (source_id,)
            )

        os.replace(m3u_candidate, m3u_cache)
        if epg_count is not None:
            os.replace(xml_candidate, xml_cache)
            rebuild_epg_index(source_id, xml_cache)
        else:
            epg_count = 0
            with connect() as conn:
                conn.execute('DELETE FROM epg_channels WHERE source_id=?', (source_id,))

        msg = f'{count} channels; {new_count} new; {removed_count} removed; {changed_count} changed; {epg_count} EPG channels'
        with connect() as conn:
            conn.execute("UPDATE sources SET last_status='OK',last_refresh=?,last_error=NULL,channel_count=? WHERE id=?", (now,count,source_id))
            conn.execute("UPDATE refresh_log SET finished_at=?,status='ok',message=? WHERE id=?", (now,msg,log_id))
        _trim_history()
        gc.collect()
        return {'source_id':source_id,'status':'ok','channel_count':count,'new':new_count,'removed':removed_count,'changed':changed_count,'epg_channels':epg_count}
    except Exception as exc:
        m3u_candidate.unlink(missing_ok=True)
        xml_candidate.unlink(missing_ok=True)
        now = utc_now()
        with connect() as conn:
            conn.execute("UPDATE sources SET last_status='ERROR',last_refresh=?,last_error=? WHERE id=?", (now,str(exc),source_id))
            conn.execute("UPDATE refresh_log SET finished_at=?,status='error',message=? WHERE id=?", (now,str(exc),log_id))
        _trim_history()
        raise


def m3u_escape(value: str | None) -> str:
    return (value or '').replace('"', "'")


def generate_master_m3u() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUTPUT_DIR / 'master.m3u'
    tmp = OUTPUT_DIR / 'master.m3u.tmp'
    count = 0
    with connect() as conn, tmp.open('w', encoding='utf-8', newline='\n') as fh:
        fh.write('#EXTM3U\n')
        cursor = conn.execute(
            '''SELECT c.* FROM channels c JOIN sources s ON s.id=c.source_id
               WHERE c.selected=1 AND c.is_active=1 AND s.enabled=1
               ORDER BY c.sort_order,c.id'''
        )
        for row in cursor:
            name = row['custom_name'] or row['name']
            tvg_id = row['custom_tvg_id'] or row['tvg_id']
            logo = row['custom_logo'] or row['logo']
            group_title = row['custom_group'] or row['group_title']
            attrs = []
            if tvg_id: attrs.append(f'tvg-id="{m3u_escape(tvg_id)}"')
            attrs.append(f'tvg-name="{m3u_escape(name)}"')
            if logo: attrs.append(f'tvg-logo="{m3u_escape(logo)}"')
            if group_title: attrs.append(f'group-title="{m3u_escape(group_title)}"')
            if row['channel_number'] is not None: attrs.append(f'tvg-chno="{row["channel_number"]}"')
            fh.write(f"#EXTINF:-1 {' '.join(attrs)},{name}\n{row['stream_url']}\n")
            count += 1
    os.replace(tmp, out)
    return count


def _iter_xml_matches(xml_path: Path, selected_ids: set[str], tag: str):
    if not xml_path.exists() or not selected_ids:
        return
    context = etree.iterparse(str(xml_path), events=('end',), tag=tag, recover=True, huge_tree=True)
    for _, elem in context:
        key = elem.get('id') if tag == 'channel' else elem.get('channel')
        if key in selected_ids:
            yield elem
        else:
            _clear_element(elem)
    del context


def generate_master_xml() -> tuple[int, int]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUTPUT_DIR / 'master.xml'
    tmp = OUTPUT_DIR / 'master.xml.tmp'
    channel_written: set[str] = set()
    channel_count = programme_count = 0

    with etree.xmlfile(str(tmp), encoding='utf-8') as xf:
        xf.write_declaration()
        with xf.element('tv', {'generator-info-name': 'IPTV Merge Manager v0.5.0'}):
            for tag in ('channel', 'programme'):
                with connect() as conn:
                    source_ids = [r['source_id'] for r in conn.execute(
                        '''SELECT DISTINCT c.source_id FROM channels c JOIN sources s ON s.id=c.source_id
                           WHERE c.selected=1 AND c.is_active=1 AND s.enabled=1
                           AND COALESCE(NULLIF(c.custom_tvg_id,''),c.tvg_id) IS NOT NULL'''
                    )]
                for source_id in source_ids:
                    with connect() as conn:
                        ids = {
                            r['tid'] for r in conn.execute(
                                '''SELECT COALESCE(NULLIF(custom_tvg_id,''),tvg_id) tid FROM channels
                                   WHERE source_id=? AND selected=1 AND is_active=1
                                   AND COALESCE(NULLIF(custom_tvg_id,''),tvg_id) IS NOT NULL''', (source_id,)
                            ) if r['tid']
                        }
                    xml_path = CACHE_DIR / f'source_{source_id}.xml'
                    for elem in _iter_xml_matches(xml_path, ids, tag) or ():
                        if tag == 'channel':
                            cid = elem.get('id')
                            if cid and cid not in channel_written:
                                xf.write(elem); channel_written.add(cid); channel_count += 1
                        else:
                            xf.write(elem); programme_count += 1
                        _clear_element(elem)
                    ids.clear()
                    gc.collect()
    os.replace(tmp, out)

    gz_tmp = OUTPUT_DIR / 'master.xml.gz.tmp'
    gz_out = OUTPUT_DIR / 'master.xml.gz'
    with out.open('rb') as src, gzip.open(gz_tmp, 'wb', compresslevel=6) as dst:
        shutil.copyfileobj(src, dst, length=CHUNK_SIZE)
    os.replace(gz_tmp, gz_out)
    gc.collect()
    return channel_count, programme_count


def generate_outputs() -> dict:
    m3u_channels = generate_master_m3u()
    xml_channels, programmes = generate_master_xml()
    gc.collect()
    return {'m3u_channels': m3u_channels, 'xml_channels': xml_channels, 'programmes': programmes}
