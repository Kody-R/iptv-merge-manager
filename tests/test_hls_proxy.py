import tempfile
import unittest
from pathlib import Path

from app.hls_proxy import (
    build_compat_master,
    build_locked_master,
    channel_diagnostics,
    effective_hls_height,
    effective_hls_mode,
    parse_master,
    resolve_playlist_token,
    resolve_segment_token,
    rewrite_media_playlist,
    select_variant,
)
from app import db, iptv


MASTER = '''#EXTM3U
#EXT-X-VERSION:3
#EXT-X-MEDIA:LANGUAGE="en",AUTOSELECT=YES,TYPE=SUBTITLES,URI="../../../manifest/root/3.m3u8",GROUP-ID="subtitles",DEFAULT=NO,NAME="English[VTT]"
#EXT-X-MEDIA:LANGUAGE="en",AUTOSELECT=NO,INSTREAM-ID="CC1",TYPE=CLOSED-CAPTIONS,GROUP-ID="captions",DEFAULT=NO,NAME="English"
#EXT-X-STREAM-INF:CODECS="avc1.4d0029,mp4a.40.2",RESOLUTION=640x360,SUBTITLES="subtitles",BANDWIDTH=921600,CLOSED-CAPTIONS="captions"
../../../manifest/root/0.m3u8
#EXT-X-STREAM-INF:CODECS="avc1.4d0029,mp4a.40.2",RESOLUTION=960x540,SUBTITLES="subtitles",BANDWIDTH=2048000,CLOSED-CAPTIONS="captions"
../../../manifest/root/1.m3u8
#EXT-X-STREAM-INF:CODECS="avc1.4d0029,mp4a.40.2",RESOLUTION=1280x720,SUBTITLES="subtitles",BANDWIDTH=3072000,CLOSED-CAPTIONS="captions"
../../../manifest/root/2.m3u8
'''
BASE = 'https://pb.example.net/v1/manifest/hash/provider/session/master.m3u8'
LOCAL = 'http://192.168.8.122:8080'


class HlsProxyTests(unittest.TestCase):
    def test_selects_720p_variant(self):
        variants = parse_master(MASTER, BASE)
        self.assertEqual(len(variants), 3)
        chosen = select_variant(variants, 720)
        self.assertEqual((chosen.width, chosen.height, chosen.bandwidth), (1280, 720, 3072000))
        self.assertTrue(chosen.absolute_uri.endswith('/manifest/root/2.m3u8'))

    def test_cap_selects_540p(self):
        self.assertEqual(select_variant(parse_master(MASTER, BASE), 540).height, 540)

    def test_highest_mode(self):
        self.assertEqual(select_variant(parse_master(MASTER, BASE), 0).height, 720)

    def test_locked_master_contains_only_selected_variant(self):
        variants = parse_master(MASTER, BASE)
        chosen = select_variant(variants, 720)
        locked = build_locked_master(MASTER, BASE, chosen)
        self.assertEqual(locked.count('#EXT-X-STREAM-INF:'), 1)
        self.assertIn('/manifest/root/2.m3u8', locked)
        self.assertNotIn('/manifest/root/0.m3u8', locked)
        self.assertNotIn('/manifest/root/1.m3u8', locked)

    def test_compat_master_routes_child_playlists_locally(self):
        compat = build_compat_master(MASTER, BASE, 3201, LOCAL)
        self.assertEqual(compat.count('#EXT-X-STREAM-INF:'), 3)
        self.assertIn(f'{LOCAL}/hls/channel/3201/playlist/', compat)
        self.assertNotIn('../../../manifest/root/0.m3u8', compat)
        token = compat.split('/playlist/', 1)[1].split('.m3u8', 1)[0]
        self.assertIsNotNone(resolve_playlist_token(3201, token))

    def test_media_playlist_is_rewritten_to_direct_cdn_urls(self):
        media = '''#EXTM3U
#EXT-X-TARGETDURATION:6
#EXT-X-KEY:METHOD=AES-128,URI="keys/key.bin"
#EXTINF:6.0,
segment001.ts
#EXTINF:6.0,
../segments/segment002.ts
'''
        url = 'https://cdn.example.net/live/channel/2.m3u8'
        rewritten = rewrite_media_playlist(media, url)
        self.assertIn('URI="https://cdn.example.net/live/channel/keys/key.bin"', rewritten)
        self.assertIn('https://cdn.example.net/live/channel/segment001.ts', rewritten)
        self.assertIn('https://cdn.example.net/live/segments/segment002.ts', rewritten)

    def test_extensionless_segment_gets_synthetic_ts_alias(self):
        media = '''#EXTM3U
#EXT-X-VERSION:6
#EXT-X-TARGETDURATION:6
#EXT-X-DISCONTINUITY
#EXTINF:6.0,
https://pb.example.net/v1/segment/hash/provider/session/2/1575659
'''
        rewritten = rewrite_media_playlist(media, BASE, 3202, LOCAL, True, 'media')
        self.assertRegex(rewritten, rf'{LOCAL}/hls/channel/3202/segment/[0-9a-f]{{24}}\.ts')
        token = rewritten.split('/segment/', 1)[1].split('.ts', 1)[0]
        entry = resolve_segment_token(3202, token)
        self.assertIsNotNone(entry)
        self.assertTrue(entry.url.endswith('/2/1575659'))
        diag = channel_diagnostics(3202)
        self.assertEqual(diag['stats'].get('extensionless_segments'), 1)
        self.assertEqual(diag['stats'].get('discontinuities'), 1)

    def test_known_ts_segment_stays_direct_even_in_compat_mode(self):
        media = '#EXTM3U\n#EXTINF:6,\nhttps://cdn.example.net/live/3000-00118.ts\n'
        rewritten = rewrite_media_playlist(media, BASE, 3203, LOCAL, True, 'media')
        self.assertIn('https://cdn.example.net/live/3000-00118.ts', rewritten)
        self.assertNotIn('/segment/', rewritten)

    def test_hls_configuration_hierarchy(self):
        self.assertEqual(effective_hls_mode(None, 'fixed', 'direct'), 'fixed')
        self.assertEqual(effective_hls_mode('compat', 'fixed', 'direct'), 'compat')
        self.assertEqual(effective_hls_mode(None, 'inherit', 'direct'), 'direct')
        self.assertEqual(effective_hls_height(None, 540, 720), 540)
        self.assertEqual(effective_hls_height(0, 540, 720), 0)

    def test_v031_boolean_lock_migrates_to_explicit_v032_modes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old_data, old_db = db.DATA_DIR, db.DB_PATH
            try:
                db.DATA_DIR = root / 'data'
                db.DB_PATH = db.DATA_DIR / 'iptv.db'
                db.init_db()
                with db.connect() as conn:
                    sid = conn.execute("INSERT INTO sources(name,m3u_kind,m3u_value) VALUES('Legacy','url','https://example.test/list.m3u')").lastrowid
                    conn.execute("INSERT INTO channels(source_id,stable_key,name,stream_url,hls_proxy_enabled) VALUES(?,?,?,?,1)", (sid,'on','Locked','https://example.test/on.m3u8'))
                    conn.execute("INSERT INTO channels(source_id,stable_key,name,stream_url,hls_proxy_enabled) VALUES(?,?,?,?,0)", (sid,'off','Direct','https://example.test/off.m3u8'))
                    conn.execute('ALTER TABLE channels DROP COLUMN hls_mode')
                    conn.execute('ALTER TABLE sources DROP COLUMN hls_mode')
                    conn.execute('ALTER TABLE sources DROP COLUMN hls_max_height')
                    conn.execute("DELETE FROM app_settings WHERE key='hls_proxy_default_mode'")
                db.init_db()
                with db.connect() as conn:
                    modes = {r['stable_key']: r['hls_mode'] for r in conn.execute('SELECT stable_key,hls_mode FROM channels')}
                    self.assertEqual(modes['on'], 'fixed')
                    self.assertEqual(modes['off'], 'direct')
                    self.assertEqual(conn.execute("SELECT hls_mode FROM sources WHERE id=?", (sid,)).fetchone()['hls_mode'], 'inherit')
            finally:
                db.DATA_DIR, db.DB_PATH = old_data, old_db

    def test_database_migration_and_master_output_proxy_token(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old_data, old_db = db.DATA_DIR, db.DB_PATH
            old_iptv_data, old_cache, old_output = iptv.DATA_DIR, iptv.CACHE_DIR, iptv.OUTPUT_DIR
            try:
                db.DATA_DIR = root / 'data'
                db.DB_PATH = db.DATA_DIR / 'iptv.db'
                iptv.DATA_DIR = db.DATA_DIR
                iptv.CACHE_DIR = db.DATA_DIR / 'cache'
                iptv.OUTPUT_DIR = root / 'output'
                db.init_db()
                with db.connect() as conn:
                    ccols = {r[1] for r in conn.execute('PRAGMA table_info(channels)')}
                    scols = {r[1] for r in conn.execute('PRAGMA table_info(sources)')}
                    self.assertIn('hls_mode', ccols)
                    self.assertIn('hls_max_height', ccols)
                    self.assertIn('hls_mode', scols)
                    self.assertIn('hls_max_height', scols)
                    sid = conn.execute(
                        "INSERT INTO sources(name,m3u_kind,m3u_value,enabled) VALUES('Test','url','https://example.test/list.m3u',1)"
                    ).lastrowid
                    conn.execute(
                        '''INSERT INTO channels(source_id,stable_key,name,stream_url,selected,sort_order,is_active,hls_proxy_enabled,hls_mode,hls_max_height)
                           VALUES(?,?,?,?,1,10,1,1,'fixed',720)''',
                        (sid, 'test', 'Problem Channel', 'https://example.test/master.m3u8'),
                    )
                count = iptv.generate_master_m3u()
                self.assertEqual(count, 1)
                output = (iptv.OUTPUT_DIR / 'master.m3u').read_text()
                self.assertIn('__IPTVMM_BASE__/hls/channel/', output)
                self.assertNotIn('https://example.test/master.m3u8\n', output)
            finally:
                db.DATA_DIR, db.DB_PATH = old_data, old_db
                iptv.DATA_DIR, iptv.CACHE_DIR, iptv.OUTPUT_DIR = old_iptv_data, old_cache, old_output


if __name__ == '__main__':
    unittest.main()
