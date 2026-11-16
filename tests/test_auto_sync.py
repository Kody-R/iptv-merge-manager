"""Offline integration tests for v0.5.1 source-sync behavior and disk-backed outputs."""
from __future__ import annotations

import asyncio
import gzip
import importlib
from pathlib import Path

import pytest
from lxml import etree


def playlist(channels: list[str]) -> str:
    text = '#EXTM3U\n'
    for channel in channels:
        text += (f'#EXTINF:-1 tvg-id="{channel}" tvg-name="{channel}" '
                 f'group-title="Sports",{channel}\nhttps://example.test/{channel}.m3u8\n')
    return text


def guide(channels: list[str]) -> str:
    return ('<?xml version="1.0"?><tv>'
            + ''.join(f'<channel id="{c}"><display-name>{c}</display-name></channel>'
                      f'<programme channel="{c}" start="20261116000000 +0000" stop="20261116003000 +0000">'
                      f'<title>{c} show</title></programme>' for c in channels)
            + '</tv>')


@pytest.fixture
def installed(tmp_path: Path, monkeypatch):
    monkeypatch.setenv('IPTVMM_DATA_DIR', str(tmp_path/'data'))
    monkeypatch.setenv('IPTVMM_OUTPUT_DIR', str(tmp_path/'output'))
    from app import db, iptv
    importlib.reload(db)
    importlib.reload(iptv)
    db.init_db()
    return db, iptv, tmp_path


def create_source(db, folder, name: str, channels: list[str], mode='manual', xml=True):
    src = folder/f'{name}.m3u'
    src.write_text(playlist(channels))
    epg = folder/f'{name}.xml'
    epg.write_text(guide(channels))
    with db.connect() as c:
        sid = c.execute('''INSERT INTO sources(name,m3u_kind,m3u_value,xml_kind,xml_value,selection_mode)
                          VALUES(?,?,?,?,?,?)''', (name,'file',str(src),'file' if xml else None,str(epg) if xml else None,mode)).lastrowid
    return sid, src, epg


def get_channels(db, sid: int) -> dict[str, dict]:
    with db.connect() as c:
        return {r['tvg_id']: dict(r) for r in c.execute('SELECT * FROM channels WHERE source_id=?', (sid,))}


def test_dynamic_sync_outputs_and_returns(installed):
    db, iptv, td = installed
    mid, mp, me = create_source(db, td, 'manual', ['man1', 'man2'])
    sid, src, epg = create_source(db, td, 'dynamic', ['alpha', 'bravo', 'charlie'], mode='all')
    asyncio.run(iptv.refresh_source(mid))
    result = asyncio.run(iptv.refresh_source(sid))
    assert result['auto_included'] == 3
    assert result['auto_numbered'] == 3
    auto = get_channels(db, sid)
    assert [auto[c]['channel_number'] for c in ['alpha', 'bravo', 'charlie']] == [1,2,3]
    assert all(c['selected'] == 1 for c in auto.values())
    assert all(c['selected'] == 0 for c in get_channels(db, mid).values())
    with db.connect() as c:
        c.execute("UPDATE channels SET custom_name='Custom Alpha',channel_number=99 WHERE source_id=? AND tvg_id='alpha'", (sid,))
        c.execute("UPDATE channels SET selected=1,channel_number=11 WHERE source_id=? AND tvg_id='man1'", (mid,))
    src.write_text(playlist(['alpha','charlie','delta']))
    epg.write_text(guide(['alpha','charlie','delta']))
    result = asyncio.run(iptv.refresh_source(sid))
    assert result['new'] == 1 and result['removed'] == 1
    assert result['auto_numbered'] == 1
    auto = get_channels(db,sid)
    assert not auto['bravo']['is_active']
    assert auto['bravo']['channel_number'] == 2
    assert auto['charlie']['channel_number'] == 3
    assert auto['alpha']['channel_number'] == 99
    assert auto['alpha']['custom_name'] == 'Custom Alpha'
    assert auto['delta']['selected'] == 1 and auto['delta']['channel_number'] == 100
    generated = iptv.generate_outputs()
    assert generated['m3u_channels'] == 4  # 3 dynamic + 1 curated
    master = (td/'output'/'master.m3u').read_text()
    assert 'Custom Alpha' in master and 'man1' in master
    assert 'bravo' not in master and 'man2' not in master
    xmlpath = td/'output'/'master.xml'
    xml = etree.parse(str(xmlpath))
    assert {e.get('id') for e in xml.getroot().findall('channel')} == {'alpha','charlie','delta','man1'}
    assert {e.get('channel') for e in xml.getroot().findall('programme')} == {'alpha','charlie','delta','man1'}
    assert gzip.decompress((td/'output'/'master.xml.gz').read_bytes()) == xmlpath.read_bytes()
    src.write_text(playlist(['alpha','bravo','charlie','delta']))
    epg.write_text(guide(['alpha','bravo','charlie','delta']))
    asyncio.run(iptv.refresh_source(sid))
    assert get_channels(db, sid)['bravo']['is_active'] == 1
    assert get_channels(db, sid)['bravo']['channel_number'] == 2


def test_manual_source_new_channel_remains_unselected(installed):
    db, iptv, td = installed
    sid, src, epg = create_source(db, td, 'manual', ['one','two'])
    asyncio.run(iptv.refresh_source(sid))
    src.write_text(playlist(['one','two','three']))
    epg.write_text(guide(['one','two','three']))
    asyncio.run(iptv.refresh_source(sid))
    assert not any(c['selected'] for c in get_channels(db,sid).values())
    assert iptv.generate_outputs()['m3u_channels'] == 0


def test_large_drop_rejected_last_known_good_preserved(installed):
    db, iptv, td = installed
    all_channels = [f'c{i:02}' for i in range(24)]
    sid, src, epg = create_source(db, td, 'dynamic', all_channels, mode='all')
    asyncio.run(iptv.refresh_source(sid))
    cached = (td/'data'/'cache'/f'source_{sid}.m3u').read_bytes()
    src.write_text(playlist(all_channels[:5]))
    epg.write_text(guide(all_channels[:5]))
    with pytest.raises(ValueError, match='sudden channel loss'):
        asyncio.run(iptv.refresh_source(sid))
    assert (td/'data'/'cache'/f'source_{sid}.m3u').read_bytes() == cached
    assert len([c for c in get_channels(db,sid).values() if c['is_active']]) == 24
    assert iptv.generate_outputs()['m3u_channels'] == 24


def test_migration_adds_manual_to_existing_sources(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setenv('IPTVMM_DATA_DIR', str(tmp_path/'data'))
    monkeypatch.setenv('IPTVMM_OUTPUT_DIR', str(tmp_path/'output'))
    (tmp_path/'data').mkdir()
    # A real pre-v0.5.1 SQLite sources table, without selection_mode.
    conn = sqlite3.connect(tmp_path/'data'/'iptv.db')
    conn.executescript("""CREATE TABLE sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
        m3u_kind TEXT NOT NULL CHECK(m3u_kind IN ('url','file')),
        m3u_value TEXT NOT NULL, xml_kind TEXT, xml_value TEXT,
        enabled INTEGER NOT NULL DEFAULT 1,
        last_status TEXT NOT NULL DEFAULT 'Never refreshed',
        last_refresh TEXT, last_error TEXT,
        channel_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        INSERT INTO sources(name,m3u_kind,m3u_value)
        VALUES('legacy','url','https://example.test/list.m3u');""")
    conn.commit()
    conn.close()
    from app import db
    importlib.reload(db)
    db.init_db()
    db.init_db()  # idempotent
    with db.connect() as c:
        source = c.execute('SELECT selection_mode FROM sources WHERE id=1').fetchone()
        c.execute("INSERT INTO channels(source_id,stable_key,name,stream_url,selected,channel_number) VALUES(1,'tvg:old','Old','https://example.test/old',1,15)")
        channel = c.execute('SELECT selected,channel_number FROM channels WHERE source_id=1').fetchone()
    assert source['selection_mode'] == 'manual'
    assert (channel['selected'],channel['channel_number']) == (1,15)


def test_switching_mode_via_api_is_immediate(installed, monkeypatch):
    """Exercise the PATCH handler without starting the four-hour scheduler."""
    import sys
    import types
    from importlib import import_module

    db, iptv, td = installed
    sid, src, epg = create_source(db, td, 'changing', ['one', 'two'])
    asyncio.run(iptv.refresh_source(sid))
    assert not any(ch['selected'] for ch in get_channels(db, sid).values())

    # The production image installs APScheduler, which is unavailable in this
    # offline test environment. Only the scheduler imports are stubbed; actual
    # API functions, schema validation and worker generation are exercised.
    for name in ('apscheduler', 'apscheduler.schedulers',
                 'apscheduler.schedulers.asyncio', 'apscheduler.triggers',
                 'apscheduler.triggers.cron'):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    sys.modules['apscheduler.schedulers.asyncio'].AsyncIOScheduler = lambda **kwargs: object()
    sys.modules['apscheduler.triggers.cron'].CronTrigger = lambda **kwargs: object()
    sys.modules.pop('app.main', None)
    main = import_module('app.main')

    async def generate():
        return iptv.generate_outputs()
    monkeypatch.setattr(main, 'regenerate_outputs', generate)

    result = asyncio.run(main.set_source_mode(sid, main.SourceModePatch(selection_mode='all')))
    assert result['output']['m3u_channels'] == 2
    assert sorted(ch['channel_number'] for ch in get_channels(db, sid).values()) == [1,2]
    assert all(ch['selected'] for ch in get_channels(db,sid).values())

    asyncio.run(main.set_source_mode(sid, main.SourceModePatch(selection_mode='manual')))
    src.write_text(playlist(['one', 'two', 'three']))
    epg.write_text(guide(['one', 'two', 'three']))
    asyncio.run(iptv.refresh_source(sid))
    ch = get_channels(db,sid)
    assert ch['one']['selected'] and ch['two']['selected']
    assert ch['three']['selected'] == 0
    assert iptv.generate_outputs()['m3u_channels'] == 2
