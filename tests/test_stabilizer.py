import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import db, iptv
from app.stabilizer import build_stabilizer_command, effective_stabilizer_mode


class StabilizerPolicyTests(unittest.TestCase):
    def test_configuration_hierarchy(self):
        self.assertEqual(effective_stabilizer_mode(None, 'remux', 'off'), 'remux')
        self.assertEqual(effective_stabilizer_mode('off', 'remux', 'transcode'), 'off')
        self.assertEqual(effective_stabilizer_mode(None, 'inherit', 'transcode'), 'transcode')
        self.assertEqual(effective_stabilizer_mode('remux', 'off', 'off'), 'remux')

    def test_remux_command_normalizes_without_copyts_or_re(self):
        channel = {
            'id': 139,
            'stream_url': 'https://example.test/live/master.m3u8',
            'effective_hls_mode': 'protected',
            'effective_stabilizer_mode': 'remux',
        }
        settings = {
            'ffmpeg_path': 'ffmpeg', 'dts_delta_threshold': 1.0, 'hls_time': 3,
            'hls_list_size': 12, 'hls_delete_threshold': 4,
            'x264_preset': 'veryfast', 'x264_crf': 20, 'audio_bitrate': '160k',
        }
        with tempfile.TemporaryDirectory() as td, patch('app.stabilizer.get_setting', return_value='1'):
            cmd = build_stabilizer_command(channel, settings, Path(td))
        joined = ' '.join(cmd)
        self.assertIn('-dts_delta_threshold 1.0', joined)
        self.assertIn('http://127.0.0.1:8080/hls/channel/139/index.m3u8', joined)
        self.assertIn('-c:v copy', joined)
        self.assertIn('-c:a copy', joined)
        self.assertNotIn('-copyts', cmd)
        self.assertNotIn('-re', cmd)

    def test_transcode_command_regenerates_timestamps(self):
        channel = {
            'id': 140,
            'stream_url': 'https://example.test/live/master.m3u8',
            'effective_hls_mode': 'direct',
            'effective_stabilizer_mode': 'transcode',
        }
        settings = {
            'ffmpeg_path': 'ffmpeg', 'dts_delta_threshold': 1.0, 'hls_time': 3,
            'hls_list_size': 12, 'hls_delete_threshold': 4,
            'x264_preset': 'veryfast', 'x264_crf': 20, 'audio_bitrate': '160k',
        }
        with tempfile.TemporaryDirectory() as td, patch('app.stabilizer.get_setting', return_value='1'):
            cmd = build_stabilizer_command(channel, settings, Path(td))
        self.assertIn('+genpts', cmd)
        self.assertIn('libx264', cmd)
        self.assertIn('aresample=async=1:first_pts=0', cmd)
        self.assertIn('https://example.test/live/master.m3u8', cmd)
        self.assertNotIn('-copyts', cmd)
        self.assertNotIn('-re', cmd)


class StabilizerDatabaseAndOutputTests(unittest.TestCase):
    def test_v040_schema_and_master_routes_stabilized_source(self):
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
                    self.assertIn('stabilizer_mode', ccols)
                    self.assertIn('stabilizer_mode', scols)
                    self.assertEqual(conn.execute("SELECT value FROM app_settings WHERE key='stabilizer_default_mode'").fetchone()['value'], 'off')
                    sid = conn.execute(
                        "INSERT INTO sources(name,m3u_kind,m3u_value,enabled,hls_mode,stabilizer_mode) VALUES('Samsung','url','https://example.test/list.m3u',1,'protected','remux')"
                    ).lastrowid
                    cid = conn.execute(
                        '''INSERT INTO channels(source_id,stable_key,name,stream_url,selected,sort_order,is_active,hls_mode)
                           VALUES(?,?,?,?,1,10,1,NULL)''',
                        (sid, 'samsung139', 'A&E Alaska State Troopers', 'https://example.test/channel139.m3u8'),
                    ).lastrowid
                count = iptv.generate_master_m3u()
                self.assertEqual(count, 1)
                output = (iptv.OUTPUT_DIR / 'master.m3u').read_text()
                self.assertIn(f'__IPTVMM_BASE__/stabilized/channel/{cid}/index.m3u8', output)
                self.assertNotIn('__IPTVMM_BASE__/hls/channel/', output)
            finally:
                db.DATA_DIR, db.DB_PATH = old_data, old_db
                iptv.DATA_DIR, iptv.CACHE_DIR, iptv.OUTPUT_DIR = old_iptv_data, old_cache, old_output

    def test_channel_off_override_bypasses_source_stabilizer(self):
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
                    sid = conn.execute(
                        "INSERT INTO sources(name,m3u_kind,m3u_value,enabled,hls_mode,stabilizer_mode) VALUES('Samsung','url','https://example.test/list.m3u',1,'protected','remux')"
                    ).lastrowid
                    cid = conn.execute(
                        '''INSERT INTO channels(source_id,stable_key,name,stream_url,selected,sort_order,is_active,hls_mode,stabilizer_mode)
                           VALUES(?,?,?,?,1,10,1,NULL,'off')''',
                        (sid, 'direct139', 'Override Direct', 'https://example.test/channel139.m3u8'),
                    ).lastrowid
                iptv.generate_master_m3u()
                output = (iptv.OUTPUT_DIR / 'master.m3u').read_text()
                self.assertIn(f'__IPTVMM_BASE__/hls/channel/{cid}/index.m3u8', output)
                self.assertNotIn('/stabilized/channel/', output)
            finally:
                db.DATA_DIR, db.DB_PATH = old_data, old_db
                iptv.DATA_DIR, iptv.CACHE_DIR, iptv.OUTPUT_DIR = old_iptv_data, old_cache, old_output


if __name__ == '__main__':
    unittest.main()
