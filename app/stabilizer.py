from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .db import DATA_DIR, connect, get_setting
from .hls_proxy import effective_hls_mode

VALID_STABILIZER_MODES = {'off', 'remux', 'transcode'}
STABILIZED_ROOT = DATA_DIR / 'cache' / 'stabilized'
STABILIZER_LOG_ROOT = DATA_DIR / 'logs' / 'stabilizer'
INTERNAL_BASE_URL = os.getenv('IPTVMM_INTERNAL_BASE_URL', 'http://127.0.0.1:8080').rstrip('/')
DEFAULT_USER_AGENT = os.getenv(
    'STABILIZER_USER_AGENT',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
)


def effective_stabilizer_mode(channel_mode: str | None, source_mode: str | None, global_mode: str = 'off') -> str:
    """Resolve channel -> source -> global stabilization policy."""
    c = (channel_mode or '').strip().lower()
    if c in VALID_STABILIZER_MODES:
        return c
    s = (source_mode or '').strip().lower()
    if s in VALID_STABILIZER_MODES:
        return s
    g = (global_mode or 'off').strip().lower()
    return g if g in VALID_STABILIZER_MODES else 'off'


def stabilizer_settings() -> dict[str, Any]:
    defaults = {
        'stabilizer_enabled': '1', 'stabilizer_default_mode': 'off',
        'stabilizer_idle_timeout': '60', 'stabilizer_stall_timeout': '8',
        'stabilizer_startup_timeout': '15', 'stabilizer_ready_segments': '2',
        'stabilizer_hls_time': '3', 'stabilizer_hls_list_size': '12',
        'stabilizer_hls_delete_threshold': '4', 'stabilizer_dts_delta_threshold': '1.0',
        'stabilizer_auto_restart': '1', 'stabilizer_max_workers': '12',
        'stabilizer_x264_preset': 'veryfast', 'stabilizer_x264_crf': '20',
        'stabilizer_audio_bitrate': '160k',
    }
    values = dict(defaults)
    with connect() as conn:
        for row in conn.execute("SELECT key,value FROM app_settings WHERE key LIKE 'stabilizer_%'"):
            values[row['key']] = row['value']
    return {
        'enabled': values['stabilizer_enabled'] == '1',
        'default_mode': values['stabilizer_default_mode'],
        'idle_timeout_seconds': max(15, min(3600, int(values['stabilizer_idle_timeout']))),
        'stall_timeout_seconds': max(4, min(300, int(values['stabilizer_stall_timeout']))),
        'startup_timeout_seconds': max(5, min(120, int(values['stabilizer_startup_timeout']))),
        'ready_segments': max(1, min(6, int(values['stabilizer_ready_segments']))),
        'hls_time': max(1, min(10, int(values['stabilizer_hls_time']))),
        'hls_list_size': max(4, min(60, int(values['stabilizer_hls_list_size']))),
        'hls_delete_threshold': max(1, min(20, int(values['stabilizer_hls_delete_threshold']))),
        'dts_delta_threshold': max(0.1, min(30.0, float(values['stabilizer_dts_delta_threshold']))),
        'auto_restart': values['stabilizer_auto_restart'] == '1',
        'max_workers': max(1, min(64, int(values['stabilizer_max_workers']))),
        'ffmpeg_path': os.getenv('STABILIZER_FFMPEG_PATH', 'ffmpeg'),
        'x264_preset': values['stabilizer_x264_preset'],
        'x264_crf': max(14, min(32, int(values['stabilizer_x264_crf']))),
        'audio_bitrate': values['stabilizer_audio_bitrate'],
    }


def _channel_config(channel_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            '''SELECT c.id,c.source_id,c.name,c.stream_url,c.hls_mode,c.hls_max_height,c.stabilizer_mode,
                      s.name source_name,s.hls_mode source_hls_mode,s.hls_max_height source_hls_max_height,
                      s.stabilizer_mode source_stabilizer_mode
               FROM channels c JOIN sources s ON s.id=c.source_id
               WHERE c.id=? AND c.is_active=1 AND s.enabled=1''',
            (channel_id,),
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    settings = stabilizer_settings()
    item['effective_stabilizer_mode'] = effective_stabilizer_mode(
        item.get('stabilizer_mode'), item.get('source_stabilizer_mode'), settings['default_mode']
    )
    item['effective_hls_mode'] = effective_hls_mode(
        item.get('hls_mode'), item.get('source_hls_mode'), get_setting('hls_proxy_default_mode', 'direct')
    )
    return item


def build_stabilizer_command(channel: dict[str, Any], settings: dict[str, Any], output_dir: Path) -> list[str]:
    mode = channel['effective_stabilizer_mode']
    if mode not in {'remux', 'transcode'}:
        raise ValueError(f'Channel is not configured for stabilization: {mode}')

    acquisition_mode = channel.get('effective_hls_mode') or 'direct'
    proxy_enabled = get_setting('hls_proxy_enabled', '1') == '1'
    if proxy_enabled and acquisition_mode != 'direct':
        input_url = f'{INTERNAL_BASE_URL}/hls/channel/{channel["id"]}/index.m3u8'
    else:
        input_url = str(channel['stream_url'])

    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(settings.get('ffmpeg_path') or 'ffmpeg'),
        '-hide_banner', '-nostdin', '-loglevel', 'info',
        '-dts_delta_threshold', str(settings['dts_delta_threshold']),
    ]
    if DEFAULT_USER_AGENT:
        cmd += ['-user_agent', DEFAULT_USER_AGENT]

    # Intentionally omit -copyts and -re. The stabilizer exists to allow FFmpeg to
    # repair HLS/MPEG-TS timestamp discontinuities before Jellyfin sees the stream.
    if mode == 'transcode':
        cmd += ['-fflags', '+genpts']

    cmd += ['-i', input_url, '-map', '0:v:0', '-map', '0:a:0?', '-sn', '-dn']

    if mode == 'transcode':
        cmd += [
            '-c:v', 'libx264', '-preset', str(settings['x264_preset']), '-crf', str(settings['x264_crf']),
            '-pix_fmt', 'yuv420p', '-r', '30000/1001', '-g', '90', '-keyint_min', '90', '-sc_threshold', '0',
            '-c:a', 'aac', '-b:a', str(settings['audio_bitrate']), '-af', 'aresample=async=1:first_pts=0',
        ]
    else:
        cmd += ['-c:v', 'copy', '-c:a', 'copy']

    cmd += [
        '-f', 'hls', '-hls_segment_type', 'mpegts',
        '-hls_time', str(settings['hls_time']),
        '-hls_list_size', str(settings['hls_list_size']),
        '-hls_delete_threshold', str(settings['hls_delete_threshold']),
        '-hls_start_number_source', 'epoch',
        '-hls_flags', 'delete_segments+omit_endlist+temp_file',
        '-hls_segment_filename', str(output_dir / 'segment_%010d.ts'),
        '-y', str(output_dir / 'index.m3u8'),
    ]
    return cmd


def shell_join(cmd: list[str]) -> str:
    return shlex.join(cmd)


@dataclass
class StabilizerRuntime:
    channel_id: int
    source_id: int | None = None
    name: str | None = None
    mode: str | None = None
    process: subprocess.Popen | None = None
    started_at: float | None = None
    last_access: float = field(default_factory=time.time)
    last_output_at: float | None = None
    restart_count: int = 0
    stall_count: int = 0
    start_count: int = 0
    last_exit_code: int | None = None
    last_error: str | None = None
    last_restart_reason: str | None = None
    command: list[str] = field(default_factory=list)
    log_path: str | None = None
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def status(self) -> dict[str, Any]:
        proc = self.process
        running = bool(proc and proc.poll() is None)
        return {
            'channel_id': self.channel_id,
            'source_id': self.source_id,
            'name': self.name,
            'mode': self.mode,
            'running': running,
            'pid': proc.pid if running else None,
            'started_at': self.started_at,
            'last_access': self.last_access,
            'last_output_at': self.last_output_at,
            'restart_count': self.restart_count,
            'stall_count': self.stall_count,
            'start_count': self.start_count,
            'last_exit_code': self.last_exit_code,
            'last_error': self.last_error,
            'last_restart_reason': self.last_restart_reason,
            'command': shell_join(self.command) if self.command else None,
            'log_path': self.log_path,
        }


class StabilizerSupervisor:
    def __init__(self):
        self._runtimes: dict[int, StabilizerRuntime] = {}
        self._global_lock = threading.RLock()
        self._stop = threading.Event()
        STABILIZED_ROOT.mkdir(parents=True, exist_ok=True)
        STABILIZER_LOG_ROOT.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._watchdog_loop, name='iptvmm-stabilizer-watchdog', daemon=True)
        self._thread.start()

    def runtime(self, channel_id: int) -> StabilizerRuntime:
        channel_id = int(channel_id)
        with self._global_lock:
            return self._runtimes.setdefault(channel_id, StabilizerRuntime(channel_id))

    def output_dir(self, channel_id: int) -> Path:
        return STABILIZED_ROOT / str(int(channel_id))

    def playlist_path(self, channel_id: int) -> Path:
        return self.output_dir(channel_id) / 'index.m3u8'

    def touch(self, channel_id: int) -> None:
        self.runtime(channel_id).last_access = time.time()

    def _clear_output(self, channel_id: int) -> None:
        out = self.output_dir(channel_id)
        if out.exists():
            for p in out.iterdir():
                if p.is_file() or p.is_symlink():
                    p.unlink(missing_ok=True)
                elif p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
        out.mkdir(parents=True, exist_ok=True)

    def _running_count(self) -> int:
        return sum(1 for rt in self._runtimes.values() if rt.process and rt.process.poll() is None)

    def start(self, channel_id: int, reason: str = 'viewer') -> StabilizerRuntime:
        cfg = _channel_config(channel_id)
        if not cfg:
            raise KeyError('Channel not found')
        settings = stabilizer_settings()
        if not settings['enabled']:
            raise RuntimeError('Stabilizer is globally disabled')
        if cfg['effective_stabilizer_mode'] == 'off':
            raise RuntimeError('Channel stabilization is disabled')

        rt = self.runtime(channel_id)
        with rt.lock:
            rt.last_access = time.time()
            if rt.process and rt.process.poll() is None:
                return rt
            if self._running_count() >= settings['max_workers']:
                raise RuntimeError(f'Stabilizer worker limit reached ({settings["max_workers"]})')
            if rt.process:
                rt.last_exit_code = rt.process.poll()
            self._clear_output(channel_id)
            cmd = build_stabilizer_command(cfg, settings, self.output_dir(channel_id))
            stamp = time.strftime('%Y%m%d-%H%M%S')
            log_path = STABILIZER_LOG_ROOT / f'channel-{channel_id}-{stamp}.log'
            log_fh = log_path.open('ab', buffering=0)
            log_fh.write((
                f'\n=== IPTV Merge Manager stabilizer start {time.strftime("%Y-%m-%d %H:%M:%S")} reason={reason} ===\n'
                f'CHANNEL: {cfg["name"]}\nMODE: {cfg["effective_stabilizer_mode"]}\n'
                f'ACQUISITION: {cfg["effective_hls_mode"]}\nCOMMAND: {shell_join(cmd)}\n\n'
            ).encode())
            try:
                proc = subprocess.Popen(
                    cmd, stdout=log_fh, stderr=subprocess.STDOUT,
                    start_new_session=True, close_fds=True,
                )
            except Exception as exc:
                rt.last_error = str(exc)
                raise
            finally:
                log_fh.close()
            rt.process = proc
            rt.source_id = int(cfg['source_id'])
            rt.name = str(cfg['name'])
            rt.mode = cfg['effective_stabilizer_mode']
            rt.started_at = time.time()
            rt.last_output_at = None
            rt.last_error = None
            rt.last_restart_reason = reason
            rt.command = cmd
            rt.log_path = str(log_path)
            rt.start_count += 1
            return rt

    def stop(self, channel_id: int, reason: str = 'idle') -> None:
        rt = self.runtime(channel_id)
        with rt.lock:
            proc = rt.process
            if not proc or proc.poll() is not None:
                if proc:
                    rt.last_exit_code = proc.poll()
                rt.process = None
                return
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
            rt.last_exit_code = proc.poll()
            rt.process = None
            rt.last_restart_reason = reason

    def restart(self, channel_id: int, reason: str = 'stall') -> StabilizerRuntime:
        rt = self.runtime(channel_id)
        self.stop(channel_id, reason=reason)
        rt.restart_count += 1
        if 'stall' in reason:
            rt.stall_count += 1
        rt.last_restart_reason = reason
        return self.start(channel_id, reason=reason)

    def segment_count(self, channel_id: int) -> int:
        try:
            return sum(1 for _ in self.output_dir(channel_id).glob('*.ts'))
        except FileNotFoundError:
            return 0

    def output_age(self, channel_id: int) -> float | None:
        out = self.output_dir(channel_id)
        newest: float | None = None
        try:
            for p in out.glob('*.ts'):
                m = p.stat().st_mtime
                newest = m if newest is None else max(newest, m)
            playlist = out / 'index.m3u8'
            if playlist.exists():
                m = playlist.stat().st_mtime
                newest = m if newest is None else max(newest, m)
        except FileNotFoundError:
            pass
        if newest is None:
            return None
        self.runtime(channel_id).last_output_at = newest
        return max(0.0, time.time() - newest)

    async def ensure_ready(self, channel_id: int) -> Path:
        settings = stabilizer_settings()
        self.start(channel_id)
        self.touch(channel_id)
        deadline = time.monotonic() + settings['startup_timeout_seconds']
        playlist = self.playlist_path(channel_id)
        while time.monotonic() < deadline:
            if playlist.exists() and self.segment_count(channel_id) >= settings['ready_segments']:
                return playlist
            rt = self.runtime(channel_id)
            if rt.process and rt.process.poll() is not None:
                rt.last_exit_code = rt.process.returncode
                rt.process = None
                raise RuntimeError(f'FFmpeg exited during stabilizer startup with code {rt.last_exit_code}')
            await asyncio.sleep(0.20)
        raise TimeoutError(
            f'Channel {channel_id} did not produce {settings["ready_segments"]} stabilized segments '
            f'within {settings["startup_timeout_seconds"]}s'
        )

    def status_for(self, channel_id: int) -> dict[str, Any]:
        cfg = _channel_config(channel_id)
        rt = self.runtime(channel_id)
        result = rt.status()
        result['output_age_seconds'] = self.output_age(channel_id)
        result['segment_count'] = self.segment_count(channel_id)
        if cfg:
            result['configured_mode'] = cfg.get('stabilizer_mode')
            result['source_mode'] = cfg.get('source_stabilizer_mode')
            result['effective_mode'] = cfg.get('effective_stabilizer_mode')
            result['acquisition_mode'] = cfg.get('effective_hls_mode')
            result['source_name'] = cfg.get('source_name')
        return result

    def all_status(self) -> list[dict[str, Any]]:
        return [self.status_for(cid) for cid in sorted(self._runtimes)]

    def summary(self) -> dict[str, Any]:
        items = self.all_status()
        return {
            'active_workers': sum(1 for x in items if x['running']),
            'known_workers': len(items),
            'restarts': sum(int(x['restart_count']) for x in items),
            'stalls': sum(int(x['stall_count']) for x in items),
            'errors': sum(1 for x in items if x.get('last_error')),
            'workers': items,
        }

    def _watchdog_loop(self) -> None:
        while not self._stop.wait(1.0):
            try:
                settings = stabilizer_settings()
            except Exception:
                continue
            now = time.time()
            for cid, rt in list(self._runtimes.items()):
                proc = rt.process
                if not proc:
                    continue
                if proc.poll() is not None:
                    rt.last_exit_code = proc.returncode
                    rt.process = None
                    continue
                if now - rt.last_access > settings['idle_timeout_seconds']:
                    self.stop(cid, reason='idle')
                    continue
                age = self.output_age(cid)
                if age is None:
                    if rt.started_at and now - rt.started_at > settings['startup_timeout_seconds'] and settings['auto_restart']:
                        try:
                            self.restart(cid, reason='startup-stall')
                        except Exception as exc:
                            rt.last_error = f'restart failed: {exc}'
                    continue
                if age > settings['stall_timeout_seconds'] and settings['auto_restart']:
                    try:
                        self.restart(cid, reason=f'output-stall-{age:.1f}s')
                    except Exception as exc:
                        rt.last_error = f'restart failed: {exc}'

    def shutdown(self) -> None:
        self._stop.set()
        for cid in list(self._runtimes):
            self.stop(cid, reason='shutdown')
