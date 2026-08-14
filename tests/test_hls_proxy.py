import tempfile
import unittest
from pathlib import Path

from app.hls_proxy import build_locked_master, parse_master, rewrite_media_playlist, select_variant
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


class HlsProxyTests(unittest.TestCase):
    def test_selects_720p_variant(self):
        variants = parse_master(MASTER, BASE)
        self.assertEqual(len(variants), 3)
        chosen = select_variant(variants, 720)
        self.assertEqual((chosen.width, chosen.height, chosen.bandwidth), (1280, 720, 3072000))
        self.assertTrue(chosen.absolute_uri.endswith('/manifest/root/2.m3u8'))

    def test_cap_selects_540p(self):
        chosen = select_variant(parse_master(MASTER, BASE), 540)
        self.assertEqual(chosen.height, 540)

    def test_highest_mode(self):
        chosen = select_variant(parse_master(MASTER, BASE), 0)
        self.assertEqual(chosen.height, 720)

    def test_locked_master_contains_only_selected_variant(self):
        variants = parse_master(MASTER, BASE)
        chosen = select_variant(variants, 720)
        locked = build_locked_master(MASTER, BASE, chosen)
        self.assertEqual(locked.count('#EXT-X-STREAM-INF:'), 1)
        self.assertIn('/manifest/root/2.m3u8', locked)
        self.assertNotIn('/manifest/root/0.m3u8', locked)
        self.assertNotIn('/manifest/root/1.m3u8', locked)

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
        self.assertNotIn('\nsegment001.ts\n', rewritten)

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
                    cols = {r[1] for r in conn.execute('PRAGMA table_info(channels)')}
                    self.assertIn('hls_proxy_enabled', cols)
                    self.assertIn('hls_max_height', cols)
                    sid = conn.execute(
                        "INSERT INTO sources(name,m3u_kind,m3u_value,enabled) VALUES('Test','url','https://example.test/list.m3u',1)"
                    ).lastrowid
                    conn.execute(
                        """INSERT INTO channels(source_id,stable_key,name,stream_url,selected,sort_order,is_active,hls_proxy_enabled,hls_max_height)
                           VALUES(?,?,?,?,1,10,1,1,720)""",
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
