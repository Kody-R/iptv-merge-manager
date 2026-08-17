import os, sqlite3, tempfile
from pathlib import Path


def test_no_playback_modules():
    root = Path(__file__).resolve().parents[1]
    assert not (root / 'app' / 'hls_proxy.py').exists()
    assert not (root / 'app' / 'stabilizer.py').exists()
    assert not (root / 'app' / 'playback.py').exists()
    docker = (root / 'Dockerfile').read_text().lower()
    assert 'ffmpeg' not in docker


def test_generator_uses_stored_stream_url_even_with_legacy_columns():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as td:
        os.environ['IPTVMM_DATA_DIR'] = str(Path(td)/'data')
        os.environ['IPTVMM_OUTPUT_DIR'] = str(Path(td)/'output')
        # Imports read env-backed paths at module load.
        import importlib, sys
        for name in ['app.db','app.iptv']:
            sys.modules.pop(name, None)
        db = importlib.import_module('app.db')
        iptv = importlib.import_module('app.iptv')
        db.init_db()
        with db.connect() as c:
            # Simulate v0.4.x additive columns. Core should ignore them.
            cols={r[1] for r in c.execute('PRAGMA table_info(sources)')}
            if 'hls_mode' not in cols: c.execute("ALTER TABLE sources ADD COLUMN hls_mode TEXT NOT NULL DEFAULT 'protected'")
            if 'stabilizer_mode' not in cols: c.execute("ALTER TABLE sources ADD COLUMN stabilizer_mode TEXT NOT NULL DEFAULT 'remux'")
            cols={r[1] for r in c.execute('PRAGMA table_info(channels)')}
            if 'hls_mode' not in cols: c.execute("ALTER TABLE channels ADD COLUMN hls_mode TEXT")
            if 'stabilizer_mode' not in cols: c.execute("ALTER TABLE channels ADD COLUMN stabilizer_mode TEXT")
            c.execute("INSERT INTO sources(name,m3u_kind,m3u_value,enabled) VALUES('Samsung','url','http://example/list.m3u',1)")
            sid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
            c.execute("INSERT INTO channels(source_id,stable_key,stream_url,name,selected,is_active,sort_order) VALUES(?,?,?,?,?,?,?)",(sid,'tvg:test','https://jmp2.uk/test','Test',1,1,0))
        iptv.generate_master_m3u()
        out=(Path(os.environ['IPTVMM_OUTPUT_DIR'])/'master.m3u').read_text()
        assert 'https://jmp2.uk/test' in out
        assert '/stabilized/channel/' not in out
        assert '/hls/channel/' not in out
