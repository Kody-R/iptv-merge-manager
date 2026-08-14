import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.hls_proxy import (
    build_compat_master,
    build_locked_master,
    channel_diagnostics,
    effective_hls_height,
    effective_hls_mode,
    parse_master,
    prepare_segment_relay,
    proxy_stats,
    RegistryEntry,
    resolve_playlist_token,
    resolve_segment_token,
    rewrite_media_playlist,
    select_variant,
    segment_relay_headers,
    stream_segment_relay,
)
from app import db, iptv, hls_proxy


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
        self.assertEqual(entry.media_sequence, 0)
        self.assertEqual(entry.playlist_url, BASE)
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


class GuardedSegmentRelayTests(unittest.IsolatedAsyncioTestCase):
    async def _server(self, responder):
        async def handler(reader, writer):
            try:
                await reader.readuntil(b'\r\n\r\n')
                await responder(writer)
            except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
                pass
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        return server, f'http://127.0.0.1:{port}/segment'

    async def test_guarded_relay_streams_synthetic_segment(self):
        payload = b'IPTVMM-SEGMENT-' * 8192

        async def responder(writer):
            writer.write(
                b'HTTP/1.1 200 OK\r\n'
                b'Content-Type: video/mp2t\r\n'
                + f'Content-Length: {len(payload)}\r\n'.encode()
                + b'Connection: close\r\n\r\n'
                + payload
            )
            await writer.drain()

        server, url = await self._server(responder)
        try:
            entry = RegistryEntry(url=url, channel_id=9101, expires_at=time.monotonic() + 60, suffix='.ts')
            before = proxy_stats()['segment_completed']
            relay = await prepare_segment_relay(9101, entry)
            async def connected(): return False
            chunks = [chunk async for chunk in stream_segment_relay(9101, relay, connected)]
            self.assertEqual(b''.join(chunks), payload)
            self.assertEqual(segment_relay_headers(relay)['X-IPTVMM-Segment-Relay'], 'guarded')
            self.assertEqual(proxy_stats()['segment_completed'], before + 1)
        finally:
            server.close()
            await server.wait_closed()

    async def test_first_byte_stall_is_retried_then_fails_bounded(self):
        async def responder(writer):
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: video/mp2t\r\nConnection: close\r\n\r\n')
            await writer.drain()
            await asyncio.sleep(0.5)

        server, url = await self._server(responder)
        try:
            entry = RegistryEntry(url=url, channel_id=9102, expires_at=time.monotonic() + 60, suffix='.ts')
            before = proxy_stats()
            with patch.object(hls_proxy, 'SEGMENT_CONNECT_TIMEOUT', 0.05), \
                 patch.object(hls_proxy, 'SEGMENT_FIRST_BYTE_TIMEOUT', 0.05), \
                 patch.object(hls_proxy, 'SEGMENT_READ_IDLE_TIMEOUT', 0.05), \
                 patch.object(hls_proxy, 'SEGMENT_TOTAL_TIMEOUT', 0.12), \
                 patch.object(hls_proxy, 'SEGMENT_MAX_ATTEMPTS', 2):
                with self.assertRaises(RuntimeError):
                    await prepare_segment_relay(9102, entry)
            after = proxy_stats()
            self.assertGreaterEqual(after['segment_timeouts'] - before['segment_timeouts'], 2)
            self.assertEqual(after['segment_retries'] - before['segment_retries'], 1)
            self.assertEqual(after['segment_relay_failures'] - before['segment_relay_failures'], 1)
        finally:
            server.close()
            await server.wait_closed()

    async def test_failed_segment_refreshes_same_media_sequence_to_new_url(self):
        payload = b'RECOVERED-SEGMENT-' * 8192
        base = ''

        async def handler(reader, writer):
            try:
                request = await reader.readuntil(b'\r\n\r\n')
                path = request.split(b' ', 2)[1].decode()
                if path == '/old':
                    writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: video/mp2t\r\nConnection: close\r\n\r\n')
                    await writer.drain()
                    await asyncio.sleep(0.4)
                elif path == '/playlist.m3u8':
                    body = (
                        '#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:500\n#EXT-X-TARGETDURATION:6\n'
                        '#EXTINF:6.0,\n' + base + '/new\n'
                    ).encode()
                    writer.write(
                        b'HTTP/1.1 200 OK\r\nContent-Type: application/vnd.apple.mpegurl\r\n'
                        + f'Content-Length: {len(body)}\r\n'.encode()
                        + b'Connection: close\r\n\r\n' + body
                    )
                    await writer.drain()
                elif path == '/new':
                    writer.write(
                        b'HTTP/1.1 200 OK\r\nContent-Type: video/mp2t\r\n'
                        + f'Content-Length: {len(payload)}\r\n'.encode()
                        + b'Connection: close\r\n\r\n' + payload
                    )
                    await writer.drain()
                else:
                    writer.write(b'HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                    await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
                pass
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        try:
            entry = RegistryEntry(
                url=base + '/old', channel_id=9105, expires_at=time.monotonic() + 60,
                suffix='.ts', playlist_url=base + '/playlist.m3u8', media_sequence=500,
            )
            before = proxy_stats()['segment_url_recoveries']
            with patch.object(hls_proxy, 'SEGMENT_CONNECT_TIMEOUT', 0.05), \
                 patch.object(hls_proxy, 'SEGMENT_FIRST_BYTE_TIMEOUT', 0.05), \
                 patch.object(hls_proxy, 'SEGMENT_READ_IDLE_TIMEOUT', 0.05), \
                 patch.object(hls_proxy, 'SEGMENT_TOTAL_TIMEOUT', 0.20), \
                 patch.object(hls_proxy, 'SEGMENT_PLAYLIST_REFRESH_TIMEOUT', 0.20), \
                 patch.object(hls_proxy, 'SEGMENT_MAX_ATTEMPTS', 2):
                relay = await prepare_segment_relay(9105, entry)
                async def connected(): return False
                chunks = [chunk async for chunk in stream_segment_relay(9105, relay, connected)]
            self.assertEqual(relay.attempt, 2)
            self.assertTrue(relay.final_url.endswith('/new'))
            self.assertEqual(b''.join(chunks), payload)
            self.assertEqual(proxy_stats()['segment_url_recoveries'], before + 1)
        finally:
            server.close()
            await server.wait_closed()

    async def test_mid_segment_idle_watchdog_closes_stream(self):
        first = b'X' * hls_proxy.SEGMENT_CHUNK_BYTES

        async def responder(writer):
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: video/mp2t\r\nConnection: close\r\n\r\n' + first)
            await writer.drain()
            await asyncio.sleep(0.5)

        server, url = await self._server(responder)
        try:
            entry = RegistryEntry(url=url, channel_id=9103, expires_at=time.monotonic() + 60, suffix='.ts')
            before = proxy_stats()['segment_timeouts']
            with patch.object(hls_proxy, 'SEGMENT_READ_IDLE_TIMEOUT', 0.05), \
                 patch.object(hls_proxy, 'SEGMENT_TOTAL_TIMEOUT', 0.15), \
                 patch.object(hls_proxy, 'SEGMENT_MAX_ATTEMPTS', 1):
                relay = await prepare_segment_relay(9103, entry)
                async def connected(): return False
                chunks = [chunk async for chunk in stream_segment_relay(9103, relay, connected)]
            self.assertEqual(b''.join(chunks), first)
            self.assertGreaterEqual(proxy_stats()['segment_timeouts'], before + 1)
        finally:
            server.close()
            await server.wait_closed()

    async def test_downstream_disconnect_cancels_upstream_relay(self):
        first = b'Y' * hls_proxy.SEGMENT_CHUNK_BYTES

        async def responder(writer):
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: video/mp2t\r\nConnection: close\r\n\r\n' + first)
            await writer.drain()
            await asyncio.sleep(0.5)

        server, url = await self._server(responder)
        try:
            entry = RegistryEntry(url=url, channel_id=9104, expires_at=time.monotonic() + 60, suffix='.ts')
            relay = await prepare_segment_relay(9104, entry)
            calls = 0
            async def disconnected():
                nonlocal calls
                calls += 1
                return calls >= 2
            before = proxy_stats()['segment_disconnects']
            chunks = [chunk async for chunk in stream_segment_relay(9104, relay, disconnected)]
            self.assertEqual(b''.join(chunks), first)
            self.assertEqual(proxy_stats()['segment_disconnects'], before + 1)
        finally:
            server.close()
            await server.wait_closed()



class ProtectedPlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def _server(self, responder):
        async def handler(reader, writer):
            try:
                await reader.readuntil(b'\r\n\r\n')
                await responder(writer)
            except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
                pass
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        return server, f'http://127.0.0.1:{port}/segment.ts'

    async def test_protected_mode_rewrites_ordinary_ts_segments(self):
        manifest = '''#EXTM3U
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:100
#EXTINF:6.0,
https://cdn.example.net/live/100.ts
#EXTINF:6.0,
https://cdn.example.net/live/101.ts
'''
        with patch.object(hls_proxy, '_protected_config', {**hls_proxy._protected_config, 'prefetch_depth': 0}):
            rewritten = rewrite_media_playlist(manifest, 'https://cdn.example.net/live/index.m3u8', 9201, LOCAL, True, 'media', relay_all=True)
        self.assertNotIn('https://cdn.example.net/live/100.ts\n', rewritten)
        self.assertEqual(rewritten.count('/hls/channel/9201/segment/'), 2)
        first_url = next(line for line in rewritten.splitlines() if '/segment/' in line)
        token = first_url.rsplit('/', 1)[1].split('.', 1)[0]
        entry = resolve_segment_token(9201, token)
        self.assertIsNotNone(entry)
        self.assertTrue(entry.protected)
        self.assertEqual(entry.media_sequence, 100)

    async def test_atomic_download_completes_before_file_is_served_and_then_hits_cache(self):
        payload = (b'\x47' + b'A' * 187) * 128
        requests = 0

        async def responder(writer):
            nonlocal requests
            requests += 1
            writer.write(
                b'HTTP/1.1 200 OK\r\nContent-Type: video/mp2t\r\n'
                + f'Content-Length: {len(payload)}\r\n'.encode()
                + b'Connection: close\r\n\r\n'
                + payload
            )
            await writer.drain()

        server, url = await self._server(responder)
        try:
            with tempfile.TemporaryDirectory() as td, \
                 patch.object(hls_proxy, 'PROTECTED_CACHE_DIR', Path(td)), \
                 patch.object(hls_proxy, '_protected_config', {**hls_proxy._protected_config, 'prefetch_depth': 0, 'segment_timeout': 2.0, 'retries': 1, 'retention_seconds': 180}):
                token = 'atomic-success'
                entry = RegistryEntry(url=url, channel_id=9202, expires_at=time.monotonic()+60, suffix='.ts', protected=True)
                first = await hls_proxy.acquire_protected_segment(9202, token, entry)
                self.assertTrue(first.path.exists())
                self.assertEqual(first.path.read_bytes(), payload)
                self.assertFalse(first.cache_hit)
                second = await hls_proxy.acquire_protected_segment(9202, token, entry)
                self.assertTrue(second.cache_hit)
                self.assertEqual(requests, 1)
        finally:
            server.close()
            await server.wait_closed()


    async def test_protected_cache_cleanup_enforces_retention_and_limit(self):
        with tempfile.TemporaryDirectory() as td, \
             patch.object(hls_proxy, 'PROTECTED_CACHE_DIR', Path(td)), \
             patch.object(hls_proxy, '_protected_config', {**hls_proxy._protected_config, 'cache_limit_mb': 1, 'retention_seconds': 60, 'segment_timeout': 5}):
            root = Path(td) / '9204'
            root.mkdir(parents=True)
            old = root / 'old.ts'
            old.write_bytes(b'X' * 128)
            os_time = time.time() - 120
            import os
            os.utime(old, (os_time, os_time))
            a = root / 'a.ts'; b = root / 'b.ts'
            a.write_bytes(b'A' * 700_000); b.write_bytes(b'B' * 700_000)
            hls_proxy._cleanup_protected_cache_sync()
            self.assertFalse(old.exists())
            total = sum(x.stat().st_size for x in Path(td).rglob('*') if x.is_file())
            self.assertLessEqual(total, 900_000)

    async def test_mid_segment_stall_never_publishes_partial_cache_file(self):
        partial = (b'\x47' + b'B' * 187) * 8

        async def responder(writer):
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: video/mp2t\r\nContent-Length: 999999\r\nConnection: close\r\n\r\n' + partial)
            await writer.drain()
            await asyncio.sleep(0.5)

        server, url = await self._server(responder)
        try:
            with tempfile.TemporaryDirectory() as td, \
                 patch.object(hls_proxy, 'PROTECTED_CACHE_DIR', Path(td)), \
                 patch.object(hls_proxy, 'SEGMENT_READ_IDLE_TIMEOUT', 0.05), \
                 patch.object(hls_proxy, '_protected_config', {**hls_proxy._protected_config, 'prefetch_depth': 0, 'segment_timeout': 0.15, 'retries': 1, 'retention_seconds': 180}):
                token = 'atomic-stall'
                entry = RegistryEntry(url=url, channel_id=9203, expires_at=time.monotonic()+60, suffix='.ts', protected=True)
                with self.assertRaises(RuntimeError):
                    await hls_proxy.acquire_protected_segment(9203, token, entry)
                final = Path(td) / '9203' / f'{token}.ts'
                self.assertFalse(final.exists())
                self.assertFalse(list(Path(td).rglob('*.part')))
        finally:
            server.close()
            await server.wait_closed()


class V034MigrationTests(unittest.TestCase):
    def test_existing_v033_fixed_modes_migrate_once_to_protected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old_data, old_db = db.DATA_DIR, db.DB_PATH
            try:
                db.DATA_DIR = root / 'data'
                db.DB_PATH = db.DATA_DIR / 'iptv.db'
                db.init_db()
                with db.connect() as conn:
                    sid = conn.execute("INSERT INTO sources(name,m3u_kind,m3u_value,hls_mode) VALUES('Old','url','https://example.test/list.m3u','fixed')").lastrowid
                    conn.execute("INSERT INTO channels(source_id,stable_key,name,stream_url,hls_proxy_enabled,hls_mode) VALUES(?,?,?,?,1,'fixed')", (sid,'fixed','Fixed','https://example.test/fixed.m3u8'))
                    conn.execute("DELETE FROM app_settings WHERE key='hls_v034_protected_migrated'")
                    conn.execute("UPDATE app_settings SET value='fixed' WHERE key='hls_proxy_default_mode'")
                db.init_db()
                with db.connect() as conn:
                    self.assertEqual(conn.execute("SELECT hls_mode FROM channels WHERE stable_key='fixed'").fetchone()['hls_mode'], 'protected')
                    self.assertEqual(conn.execute("SELECT hls_mode FROM sources WHERE id=?", (sid,)).fetchone()['hls_mode'], 'protected')
                    self.assertEqual(conn.execute("SELECT value FROM app_settings WHERE key='hls_proxy_default_mode'").fetchone()['value'], 'protected')
                    self.assertEqual(conn.execute("SELECT value FROM app_settings WHERE key='hls_v034_protected_migrated'").fetchone()['value'], '1')
            finally:
                db.DATA_DIR, db.DB_PATH = old_data, old_db


if __name__ == '__main__':
    unittest.main()
