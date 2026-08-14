from __future__ import annotations

import asyncio
import hashlib
import os
import re
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

APP_VERSION = '0.3.4'
SEGMENT_CONNECT_TIMEOUT = max(1.0, float(os.getenv('HLS_SEGMENT_CONNECT_TIMEOUT', '4')))
SEGMENT_FIRST_BYTE_TIMEOUT = max(1.0, float(os.getenv('HLS_SEGMENT_FIRST_BYTE_TIMEOUT', '6')))
SEGMENT_READ_IDLE_TIMEOUT = max(1.0, float(os.getenv('HLS_SEGMENT_READ_IDLE_TIMEOUT', '10')))
SEGMENT_TOTAL_TIMEOUT = max(5.0, float(os.getenv('HLS_SEGMENT_TOTAL_TIMEOUT', '20')))
SEGMENT_PLAYLIST_REFRESH_TIMEOUT = max(1.0, float(os.getenv('HLS_SEGMENT_PLAYLIST_REFRESH_TIMEOUT', '4')))
SEGMENT_MAX_ATTEMPTS = max(1, min(4, int(os.getenv('HLS_SEGMENT_MAX_ATTEMPTS', '2'))))
SEGMENT_CHUNK_BYTES = max(16 * 1024, min(1024 * 1024, int(os.getenv('HLS_SEGMENT_CHUNK_BYTES', str(128 * 1024)))))
DEFAULT_MAX_HEIGHT = 720
DEFAULT_CACHE_SECONDS = 15
MAX_MANIFEST_BYTES = 512 * 1024
SEGMENT_TOKEN_TTL = max(30, int(os.getenv('HLS_SEGMENT_TOKEN_TTL', '120')))
PLAYLIST_TOKEN_TTL = max(300, int(os.getenv('HLS_PLAYLIST_TOKEN_TTL', '21600')))
MAX_SEGMENT_TOKENS = max(256, int(os.getenv('HLS_MAX_SEGMENT_TOKENS', '8192')))
MAX_PLAYLIST_TOKENS = max(64, int(os.getenv('HLS_MAX_PLAYLIST_TOKENS', '1024')))
EVENT_HISTORY_LIMIT = max(10, min(200, int(os.getenv('HLS_EVENT_HISTORY_LIMIT', '50'))))
DEFAULT_USER_AGENT = os.getenv(
    'HLS_PROXY_USER_AGENT',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
)

PROTECTED_CACHE_DIR = Path(os.getenv('HLS_PROTECTED_CACHE_DIR', '/app/data/cache/hls-segments'))
PROTECTED_DEFAULT_PREFETCH = max(0, min(8, int(os.getenv('HLS_PROTECTED_PREFETCH', '2'))))
PROTECTED_DEFAULT_TIMEOUT = max(5.0, float(os.getenv('HLS_PROTECTED_SEGMENT_TIMEOUT', '15')))
PROTECTED_DEFAULT_RETRIES = max(1, min(4, int(os.getenv('HLS_PROTECTED_RETRIES', '2'))))
PROTECTED_DEFAULT_CACHE_LIMIT_MB = max(64, min(4096, int(os.getenv('HLS_PROTECTED_CACHE_LIMIT_MB', '512'))))
PROTECTED_DEFAULT_RETENTION = max(30, min(3600, int(os.getenv('HLS_PROTECTED_CACHE_RETENTION', '180'))))
PROTECTED_DEFAULT_SKIP_FAILED = os.getenv('HLS_PROTECTED_SKIP_FAILED', '1').strip().lower() not in {'0', 'false', 'no', 'off'}
PROTECTED_MAX_SEGMENT_BYTES = max(4 * 1024 * 1024, min(256 * 1024 * 1024, int(os.getenv('HLS_PROTECTED_MAX_SEGMENT_BYTES', str(64 * 1024 * 1024)))))

VALID_HLS_MODES = {'direct', 'compat', 'fixed', 'protected'}
ATTR_RE = re.compile(r'([A-Z0-9-]+)=(?:"([^"]*)"|([^,]*))', re.I)
URI_ATTR_RE = re.compile(r'URI="([^"]+)"', re.I)
SAFE_MEDIA_EXTENSIONS = {
    '.ts', '.m2ts', '.mts', '.m4s', '.mp4', '.m4a', '.aac', '.mp3', '.ac3', '.eac3',
    '.vtt', '.webvtt', '.srt', '.ttml', '.cmfv', '.cmfa', '.bin', '.key',
}


@dataclass(frozen=True)
class Variant:
    info_line: str
    uri: str
    absolute_uri: str
    width: int | None
    height: int | None
    bandwidth: int | None
    attrs: dict[str, str]

    def as_dict(self) -> dict:
        return {
            'width': self.width,
            'height': self.height,
            'bandwidth': self.bandwidth,
            'uri': self.absolute_uri,
        }


@dataclass
class CacheEntry:
    expires_at: float
    manifest: str
    final_url: str
    variants: list[Variant]


@dataclass
class RegistryEntry:
    url: str
    channel_id: int
    expires_at: float
    kind: str = 'media'
    suffix: str = '.ts'
    playlist_url: str | None = None
    media_sequence: int | None = None
    protected: bool = False
    mode: str = 'compat'


@dataclass
class PreparedSegmentRelay:
    client: httpx.AsyncClient
    response: httpx.Response
    iterator: object
    first_chunk: bytes
    deadline: float
    attempt: int
    final_url: str
    content_type: str
    content_length: str | None


@dataclass(frozen=True)
class ProtectedSegmentResult:
    path: Path
    content_type: str
    size: int
    attempt: int
    final_url: str
    cache_hit: bool = False


_cache: dict[str, CacheEntry] = {}
_cache_lock = asyncio.Lock()
_registry_lock = threading.Lock()
_segment_registry: dict[str, RegistryEntry] = {}
_playlist_registry: dict[str, RegistryEntry] = {}
_stats = {
    'requests': 0,
    'master_resolves': 0,
    'cache_hits': 0,
    'bypasses': 0,
    'failures': 0,
    'playlist_requests': 0,
    'segment_redirects': 0,  # retained as a legacy v0.3.2 counter
    'segment_relays': 0,
    'segment_retries': 0,
    'segment_timeouts': 0,
    'segment_disconnects': 0,
    'segment_completed': 0,
    'segment_relay_failures': 0,
    'segment_playlist_refreshes': 0,
    'segment_url_recoveries': 0,
    'segment_bytes': 0,
    'protected_requests': 0,
    'protected_downloads': 0,
    'protected_cache_hits': 0,
    'protected_prefetches': 0,
    'protected_prefetch_failures': 0,
    'protected_retries': 0,
    'protected_timeouts': 0,
    'protected_failures': 0,
    'protected_skips': 0,
    'protected_completed': 0,
    'protected_bytes': 0,
    'protected_invalid': 0,
    'extensionless_segments': 0,
    'discontinuities': 0,
    'cdn_switches': 0,
    'variant_reresolves': 0,
}
_channel_stats: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
_channel_events: dict[int, deque[dict]] = defaultdict(lambda: deque(maxlen=EVENT_HISTORY_LIMIT))
_channel_last_cdn: dict[int, str] = {}
_channel_discontinuity_sigs: dict[int, deque[str]] = defaultdict(lambda: deque(maxlen=32))
_protected_config = {
    'prefetch_depth': PROTECTED_DEFAULT_PREFETCH,
    'segment_timeout': PROTECTED_DEFAULT_TIMEOUT,
    'retries': PROTECTED_DEFAULT_RETRIES,
    'cache_limit_mb': PROTECTED_DEFAULT_CACHE_LIMIT_MB,
    'retention_seconds': PROTECTED_DEFAULT_RETENTION,
    'skip_failed': PROTECTED_DEFAULT_SKIP_FAILED,
}
_protected_inflight: dict[tuple[int, str], asyncio.Task] = {}
_protected_inflight_lock = asyncio.Lock()
_protected_prefetch_tasks: set[asyncio.Task] = set()
_protected_prefetch_semaphore = asyncio.Semaphore(4)
_protected_last_cleanup = 0.0


def _parse_attrs(line: str) -> dict[str, str]:
    payload = line.split(':', 1)[1] if ':' in line else line
    out: dict[str, str] = {}
    for key, quoted, bare in ATTR_RE.findall(payload):
        out[key.upper()] = quoted if quoted != '' else bare.strip()
    return out


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, '') else None
    except (TypeError, ValueError):
        return None


def _resolution(attrs: dict[str, str]) -> tuple[int | None, int | None]:
    value = attrs.get('RESOLUTION', '')
    if 'x' not in value.lower():
        return None, None
    left, right = value.lower().split('x', 1)
    return _int_or_none(left), _int_or_none(right)


def parse_master(manifest: str, base_url: str) -> list[Variant]:
    lines = [line.strip() for line in manifest.replace('\r\n', '\n').replace('\r', '\n').split('\n')]
    variants: list[Variant] = []
    for idx, line in enumerate(lines):
        if not line.upper().startswith('#EXT-X-STREAM-INF:'):
            continue
        attrs = _parse_attrs(line)
        uri = None
        for nxt in lines[idx + 1:]:
            if not nxt or nxt.startswith('#'):
                continue
            uri = nxt
            break
        if not uri:
            continue
        width, height = _resolution(attrs)
        variants.append(Variant(
            info_line=line,
            uri=uri,
            absolute_uri=urljoin(base_url, uri),
            width=width,
            height=height,
            bandwidth=_int_or_none(attrs.get('BANDWIDTH')),
            attrs=attrs,
        ))
    return variants


def select_variant(variants: list[Variant], max_height: int | None) -> Variant:
    if not variants:
        raise ValueError('No HLS variants were found')
    max_height = max_height or 0
    with_height = [v for v in variants if v.height is not None]
    if max_height > 0 and with_height:
        eligible = [v for v in with_height if (v.height or 0) <= max_height]
        if eligible:
            return max(eligible, key=lambda v: (v.height or 0, v.bandwidth or 0, v.width or 0))
        return min(with_height, key=lambda v: (v.height or 10**9, v.bandwidth or 10**12))
    return max(variants, key=lambda v: (v.height or 0, v.bandwidth or 0, v.width or 0))


def normalize_mode(value: str | None, fallback: str = 'direct') -> str:
    value = (value or '').strip().lower()
    return value if value in VALID_HLS_MODES else fallback


def effective_hls_mode(channel_mode: str | None, source_mode: str | None, global_mode: str | None) -> str:
    if channel_mode and channel_mode.lower() in VALID_HLS_MODES:
        return channel_mode.lower()
    if source_mode and source_mode.lower() in VALID_HLS_MODES:
        return source_mode.lower()
    return normalize_mode(global_mode, 'direct')


def effective_hls_height(channel_height: int | None, source_height: int | None, global_height: int | None) -> int:
    for value in (channel_height, source_height, global_height):
        if value is not None:
            return max(0, min(4320, int(value)))
    return DEFAULT_MAX_HEIGHT


def _event(channel_id: int, event_type: str, message: str) -> None:
    _channel_events[channel_id].append({
        'time': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'type': event_type,
        'message': message,
    })


def _inc(key: str, channel_id: int | None = None, amount: int = 1) -> None:
    _stats[key] = _stats.get(key, 0) + amount
    if channel_id is not None:
        _channel_stats[channel_id][key] += amount


def record_event(channel_id: int, event_type: str, message: str) -> None:
    _event(channel_id, event_type, message)


def _trim_registry(registry: dict[str, RegistryEntry], max_entries: int) -> None:
    now = time.monotonic()
    expired = [k for k, v in registry.items() if v.expires_at <= now]
    for key in expired:
        registry.pop(key, None)
    if len(registry) <= max_entries:
        return
    for key, _ in sorted(registry.items(), key=lambda kv: kv[1].expires_at)[:len(registry) - max_entries]:
        registry.pop(key, None)


def _token(channel_id: int, url: str, namespace: str) -> str:
    return hashlib.sha256(f'{namespace}\0{channel_id}\0{url}'.encode()).hexdigest()[:24]


def register_segment_url(
    channel_id: int,
    url: str,
    suffix: str = '.ts',
    playlist_url: str | None = None,
    media_sequence: int | None = None,
    protected: bool = False,
) -> tuple[str, bool]:
    if not is_http_url(url):
        raise ValueError('Only HTTP/HTTPS segment URLs can be registered')
    token_identity = f'{url}\0{media_sequence if media_sequence is not None else ""}'
    token = _token(channel_id, token_identity, 'segment')
    now = time.monotonic()
    with _registry_lock:
        _trim_registry(_segment_registry, MAX_SEGMENT_TOKENS)
        is_new = token not in _segment_registry
        _segment_registry[token] = RegistryEntry(
            url, channel_id, now + SEGMENT_TOKEN_TTL, suffix=suffix,
            playlist_url=playlist_url, media_sequence=media_sequence, protected=protected,
        )
    return token, is_new


def resolve_segment_token(channel_id: int, token: str) -> RegistryEntry | None:
    with _registry_lock:
        _trim_registry(_segment_registry, MAX_SEGMENT_TOKENS)
        entry = _segment_registry.get(token)
        if not entry or entry.channel_id != channel_id:
            return None
        return entry


def register_playlist_url(channel_id: int, url: str, kind: str = 'media', mode: str = 'compat') -> str:
    if not is_http_url(url):
        raise ValueError('Only HTTP/HTTPS playlist URLs can be registered')
    token = _token(channel_id, url, 'playlist')
    now = time.monotonic()
    with _registry_lock:
        _trim_registry(_playlist_registry, MAX_PLAYLIST_TOKENS)
        _playlist_registry[token] = RegistryEntry(url, channel_id, now + PLAYLIST_TOKEN_TTL, kind=kind, suffix='.m3u8', mode=mode)
    return token


def resolve_playlist_token(channel_id: int, token: str) -> RegistryEntry | None:
    with _registry_lock:
        _trim_registry(_playlist_registry, MAX_PLAYLIST_TOKENS)
        entry = _playlist_registry.get(token)
        if not entry or entry.channel_id != channel_id:
            return None
        return entry


def _local_playlist_url(local_base: str, channel_id: int, upstream: str, kind: str = 'media', mode: str = 'compat') -> str:
    token = register_playlist_url(channel_id, upstream, kind, mode)
    return f'{local_base}/hls/channel/{channel_id}/playlist/{token}.m3u8'


def _path_suffix(url: str) -> str:
    path = urlparse(url).path.lower().rstrip('/')
    name = path.rsplit('/', 1)[-1]
    if '.' not in name:
        return ''
    return '.' + name.rsplit('.', 1)[-1]


def _needs_segment_alias(url: str) -> bool:
    return _path_suffix(url) not in SAFE_MEDIA_EXTENSIONS


def _segment_suffix(manifest: str, playlist_kind: str = 'media') -> str:
    if playlist_kind == 'subtitle':
        return '.vtt'
    if '#EXT-X-MAP:' in manifest.upper():
        return '.m4s'
    if playlist_kind == 'audio':
        return '.aac'
    return '.ts'


def _rewrite_uri_attr_absolute(line: str, base_url: str) -> str:
    return URI_ATTR_RE.sub(lambda m: f'URI="{urljoin(base_url, m.group(1))}"', line)


def build_compat_master(manifest: str, base_url: str, channel_id: int, local_base: str, mode: str = 'compat') -> str:
    """Keep adaptive choices while routing child playlists back through the lightweight compatibility layer."""
    out: list[str] = []
    expect_variant = False
    for raw in manifest.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        line = raw.strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith('#EXT-X-STREAM-INF:'):
            expect_variant = True
            out.append(line)
            continue
        if line.startswith('#'):
            if upper.startswith('#EXT-X-MEDIA:') and 'URI=' in upper:
                attrs = _parse_attrs(line)
                kind = {'SUBTITLES': 'subtitle', 'AUDIO': 'audio'}.get(attrs.get('TYPE', '').upper(), 'media')
                line = URI_ATTR_RE.sub(
                    lambda m: f'URI="{_local_playlist_url(local_base, channel_id, urljoin(base_url, m.group(1)), kind, mode)}"',
                    line,
                )
            elif upper.startswith('#EXT-X-I-FRAME-STREAM-INF:') and 'URI=' in upper:
                line = URI_ATTR_RE.sub(
                    lambda m: f'URI="{_local_playlist_url(local_base, channel_id, urljoin(base_url, m.group(1)), "media", mode)}"',
                    line,
                )
            out.append(line)
            continue
        if expect_variant:
            out.append(_local_playlist_url(local_base, channel_id, urljoin(base_url, line), 'media', mode))
            expect_variant = False
        else:
            out.append(urljoin(base_url, line))
    return '\n'.join(out) + '\n'


def build_locked_master(
    manifest: str,
    base_url: str,
    selected: Variant,
    channel_id: int | None = None,
    local_base: str | None = None,
    mode: str = 'fixed',
) -> str:
    lines = [line.strip() for line in manifest.replace('\r\n', '\n').replace('\r', '\n').split('\n')]
    version_lines = [line for line in lines if line.upper().startswith('#EXT-X-VERSION:')]
    referenced_groups: set[tuple[str, str]] = set()
    for key in ('AUDIO', 'SUBTITLES', 'VIDEO', 'CLOSED-CAPTIONS'):
        value = selected.attrs.get(key)
        if value and value.upper() != 'NONE':
            referenced_groups.add((key, value))

    media_lines: list[str] = []
    for line in lines:
        if not line.upper().startswith('#EXT-X-MEDIA:'):
            continue
        attrs = _parse_attrs(line)
        media_type = attrs.get('TYPE', '').upper()
        group_id = attrs.get('GROUP-ID', '')
        if (media_type, group_id) not in referenced_groups:
            continue
        if channel_id is not None and local_base and 'URI=' in line.upper():
            kind = {'SUBTITLES': 'subtitle', 'AUDIO': 'audio'}.get(media_type, 'media')
            line = URI_ATTR_RE.sub(
                lambda m: f'URI="{_local_playlist_url(local_base, channel_id, urljoin(base_url, m.group(1)), kind, mode)}"',
                line,
            )
        else:
            line = _rewrite_uri_attr_absolute(line, base_url)
        media_lines.append(line)

    out = ['#EXTM3U']
    out.extend(version_lines[:1])
    out.append(
        f'# IPTV Merge Manager v{APP_VERSION} fixed variant: '
        f'{selected.width or "?"}x{selected.height or "?"} @ {selected.bandwidth or "?"} bps'
    )
    out.extend(media_lines)
    out.append(selected.info_line)
    if channel_id is not None and local_base:
        out.append(_local_playlist_url(local_base, channel_id, selected.absolute_uri, 'media', mode))
    else:
        out.append(selected.absolute_uri)
    return '\n'.join(out) + '\n'


def is_http_url(url: str) -> bool:
    return urlparse(url).scheme.lower() in {'http', 'https'}


async def _fetch_manifest(url: str) -> tuple[str, str]:
    if not is_http_url(url):
        raise ValueError('HLS proxy supports HTTP/HTTPS streams only')
    timeout = httpx.Timeout(20.0, connect=10.0)
    headers = {
        'User-Agent': DEFAULT_USER_AGENT,
        'Accept': 'application/vnd.apple.mpegurl, application/x-mpegURL, text/plain, */*',
        'Cache-Control': 'no-cache',
    }
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, headers=headers) as client:
        async with client.stream('GET', url) as response:
            response.raise_for_status()
            final_url = str(response.url)
            content_type = response.headers.get('content-type', '').lower()
            likely_manifest = (
                'mpegurl' in content_type
                or 'text/' in content_type
                or final_url.lower().split('?', 1)[0].endswith('.m3u8')
                or url.lower().split('?', 1)[0].endswith('.m3u8')
            )
            data = bytearray()
            first = True
            async for chunk in response.aiter_bytes(64 * 1024):
                data.extend(chunk)
                if first:
                    first = False
                    if not likely_manifest and not bytes(data).lstrip().startswith(b'#EXTM3U'):
                        return '', final_url
                if len(data) > MAX_MANIFEST_BYTES:
                    raise ValueError('HLS manifest exceeded the 512 KiB safety limit')
            return data.decode('utf-8', errors='replace'), final_url


async def resolve_master(url: str, cache_seconds: int = DEFAULT_CACHE_SECONDS) -> CacheEntry:
    now = time.monotonic()
    async with _cache_lock:
        cached = _cache.get(url)
        if cached and cached.expires_at > now:
            _inc('cache_hits')
            return cached
    manifest, final_url = await _fetch_manifest(url)
    variants = parse_master(manifest, final_url) if manifest else []
    entry = CacheEntry(now + max(1, cache_seconds), manifest, final_url, variants)
    async with _cache_lock:
        _cache[url] = entry
    _inc('master_resolves')
    return entry


async def invalidate(url: str) -> None:
    async with _cache_lock:
        _cache.pop(url, None)


def record_request(channel_id: int | None = None) -> None:
    _inc('requests', channel_id)


def record_playlist_request(channel_id: int) -> None:
    _inc('playlist_requests', channel_id)


def record_segment_redirect(channel_id: int) -> None:
    # Kept for API/backward compatibility with v0.3.2 diagnostics. v0.3.3+ no longer
    # redirects synthetic segment aliases; those aliases use the guarded relay below.
    _inc('segment_redirects', channel_id)


def _segment_request_headers() -> dict[str, str]:
    return {
        'User-Agent': DEFAULT_USER_AGENT,
        'Accept': '*/*',
        'Cache-Control': 'no-cache',
        'Connection': 'close',
    }


def _segment_timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=SEGMENT_CONNECT_TIMEOUT,
        read=SEGMENT_READ_IDLE_TIMEOUT,
        write=SEGMENT_READ_IDLE_TIMEOUT,
        pool=SEGMENT_CONNECT_TIMEOUT,
    )


def _media_sequence_url(manifest: str, base_url: str, target_sequence: int) -> str | None:
    sequence = 0
    offset = 0
    for raw in manifest.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        line = raw.strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith('#EXT-X-MEDIA-SEQUENCE:'):
            sequence = _int_or_none(line.split(':', 1)[1].strip()) or 0
            offset = 0
            continue
        if line.startswith('#'):
            continue
        current = sequence + offset
        if current == target_sequence:
            return urljoin(base_url, line)
        offset += 1
    return None


async def refresh_segment_candidate(channel_id: int, entry: RegistryEntry, current_url: str) -> str | None:
    """Re-read the same media playlist and resolve only the same HLS media sequence.

    We intentionally do not substitute a newer sequence. If the target has rolled out of the
    live window, FFmpeg should reload the playlist itself rather than IPTVMM silently skipping
    content. This recovery exists for SSAI/CDN cases where a sequence is republished at a new URL.
    """
    if not entry.playlist_url or entry.media_sequence is None:
        return None
    _inc('segment_playlist_refreshes', channel_id)
    try:
        async with asyncio.timeout(SEGMENT_PLAYLIST_REFRESH_TIMEOUT):
            manifest, final_url = await _fetch_manifest(entry.playlist_url)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _event(channel_id, 'segment-refresh-failed', f'Could not refresh the media playlist during segment recovery: {exc}')
        return None
    if not manifest.startswith('#EXTM3U'):
        return None
    candidate = _media_sequence_url(manifest, final_url, entry.media_sequence)
    if candidate and candidate != current_url:
        # Remember the recovered URL for any repeat request to the same short-lived alias.
        # The token remains valid because it identifies the playlist occurrence, not a security secret.
        with _registry_lock:
            entry.url = candidate
            entry.playlist_url = final_url
        _inc('segment_url_recoveries', channel_id)
        _event(
            channel_id, 'segment-url-recovered',
            f'Media sequence {entry.media_sequence} moved to a new upstream URL; retrying the refreshed segment.',
        )
        return candidate
    return None


async def prepare_segment_relay(channel_id: int, entry: RegistryEntry) -> PreparedSegmentRelay:
    """Open a synthetic compatibility segment with bounded retries.

    The first media bytes are acquired before Starlette sends the response headers. This is
    deliberate: a provider that accepts the TCP/TLS request but never returns media can be
    retried or rejected with a clean 504 instead of leaving FFmpeg blocked indefinitely.
    Only synthetic compatibility aliases use this path; normal HLS media URLs stay direct.
    """
    last_exc: Exception | None = None
    current_url = entry.url
    for attempt in range(1, SEGMENT_MAX_ATTEMPTS + 1):
        last_exc = None
        client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=_segment_timeout(),
            headers=_segment_request_headers(),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=0),
        )
        response: httpx.Response | None = None
        started = time.monotonic()
        deadline = started + SEGMENT_TOTAL_TIMEOUT
        try:
            request = client.build_request('GET', current_url)
            preflight_timeout = min(SEGMENT_TOTAL_TIMEOUT, SEGMENT_CONNECT_TIMEOUT + SEGMENT_FIRST_BYTE_TIMEOUT)
            async with asyncio.timeout(preflight_timeout):
                response = await client.send(request, stream=True)
                response.raise_for_status()
                iterator = response.aiter_bytes(SEGMENT_CHUNK_BYTES)
                first_chunk = await anext(iterator)
                if not first_chunk:
                    raise RuntimeError('Upstream segment returned an empty first chunk')
            _inc('segment_relays', channel_id)
            if attempt > 1:
                _event(channel_id, 'segment-recovered', f'Compatibility segment recovered on attempt {attempt}.')
            return PreparedSegmentRelay(
                client=client,
                response=response,
                iterator=iterator,
                first_chunk=first_chunk,
                deadline=deadline,
                attempt=attempt,
                final_url=str(response.url),
                content_type=response.headers.get('content-type', 'video/mp2t'),
                content_length=response.headers.get('content-length'),
            )
        except asyncio.CancelledError:
            last_exc = RuntimeError('Downstream cancelled the compatibility segment during preflight')
            _inc('segment_disconnects', channel_id)
            _event(channel_id, 'segment-cancelled', 'Downstream playback disconnected during compatibility-segment preflight; cancelled the upstream request.')
            raise
        except (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException) as exc:
            last_exc = exc
            _inc('segment_timeouts', channel_id)
            _event(channel_id, 'segment-timeout', f'Compatibility segment timed out on attempt {attempt}; aborting the upstream request.')
        except StopAsyncIteration as exc:
            last_exc = RuntimeError('Upstream segment returned no media bytes')
        except (httpx.HTTPStatusError, httpx.RequestError, RuntimeError) as exc:
            last_exc = exc
        except Exception as exc:
            last_exc = exc
        finally:
            if last_exc is not None:
                if response is not None:
                    await response.aclose()
                await client.aclose()

        if attempt < SEGMENT_MAX_ATTEMPTS:
            candidate = await refresh_segment_candidate(channel_id, entry, current_url)
            if candidate:
                current_url = candidate
            _inc('segment_retries', channel_id)
            _event(channel_id, 'segment-retry', f'Retrying compatibility segment ({attempt + 1}/{SEGMENT_MAX_ATTEMPTS}).')
            await asyncio.sleep(0.15)

    _inc('segment_relay_failures', channel_id)
    message = str(last_exc or 'unknown upstream segment failure')
    _event(channel_id, 'segment-failed', f'Compatibility segment failed after {SEGMENT_MAX_ATTEMPTS} bounded attempt(s): {message}')
    raise RuntimeError(message)


async def stream_segment_relay(channel_id: int, relay: PreparedSegmentRelay, is_disconnected=None):
    """Yield one compatibility segment while enforcing read-idle/absolute deadlines.

    A retry is intentionally not attempted after bytes have been emitted because concatenating
    a restarted segment onto a partial response would corrupt MPEG-TS/fMP4. If the upstream
    stalls mid-segment, the response is closed so FFmpeg can fail/reload rather than wait forever.
    """
    bytes_sent = 0
    completed = False
    disconnected = False
    timed_out = False
    try:
        if is_disconnected is not None and await is_disconnected():
            disconnected = True
            return
        bytes_sent += len(relay.first_chunk)
        yield relay.first_chunk

        while True:
            if is_disconnected is not None and await is_disconnected():
                disconnected = True
                return
            remaining_total = relay.deadline - time.monotonic()
            if remaining_total <= 0:
                timed_out = True
                return
            wait_for = min(SEGMENT_READ_IDLE_TIMEOUT, remaining_total)
            try:
                chunk = await asyncio.wait_for(anext(relay.iterator), timeout=max(0.05, wait_for))
            except StopAsyncIteration:
                completed = True
                return
            except (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException):
                timed_out = True
                return
            if not chunk:
                continue
            bytes_sent += len(chunk)
            yield chunk
    except asyncio.CancelledError:
        disconnected = True
        raise
    finally:
        _inc('segment_bytes', channel_id, bytes_sent)
        if completed:
            _inc('segment_completed', channel_id)
        if disconnected:
            _inc('segment_disconnects', channel_id)
            _event(channel_id, 'segment-cancelled', 'Downstream playback disconnected; cancelled the compatibility segment fetch.')
        if timed_out:
            _inc('segment_timeouts', channel_id)
            _inc('segment_relay_failures', channel_id)
            _event(channel_id, 'segment-stalled', 'Compatibility segment stopped producing bytes; stale-segment watchdog closed the upstream request.')
        await relay.response.aclose()
        await relay.client.aclose()


def segment_relay_headers(relay: PreparedSegmentRelay) -> dict[str, str]:
    headers = {
        'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
        'Pragma': 'no-cache',
        'X-IPTVMM-Segment-Relay': 'guarded',
        'X-IPTVMM-Relay-Attempt': str(relay.attempt),
    }
    return headers


def segment_relay_settings() -> dict:
    return {
        'connect_timeout_seconds': SEGMENT_CONNECT_TIMEOUT,
        'first_byte_timeout_seconds': SEGMENT_FIRST_BYTE_TIMEOUT,
        'read_idle_timeout_seconds': SEGMENT_READ_IDLE_TIMEOUT,
        'total_timeout_seconds': SEGMENT_TOTAL_TIMEOUT,
        'playlist_refresh_timeout_seconds': SEGMENT_PLAYLIST_REFRESH_TIMEOUT,
        'max_attempts': SEGMENT_MAX_ATTEMPTS,
        'chunk_bytes': SEGMENT_CHUNK_BYTES,
    }



def configure_protected(
    prefetch_depth: int | None = None,
    segment_timeout: float | None = None,
    retries: int | None = None,
    cache_limit_mb: int | None = None,
    retention_seconds: int | None = None,
    skip_failed: bool | None = None,
) -> dict:
    if prefetch_depth is not None:
        _protected_config['prefetch_depth'] = max(0, min(8, int(prefetch_depth)))
    if segment_timeout is not None:
        _protected_config['segment_timeout'] = max(5.0, min(60.0, float(segment_timeout)))
    if retries is not None:
        _protected_config['retries'] = max(1, min(4, int(retries)))
    if cache_limit_mb is not None:
        _protected_config['cache_limit_mb'] = max(64, min(4096, int(cache_limit_mb)))
    if retention_seconds is not None:
        _protected_config['retention_seconds'] = max(30, min(3600, int(retention_seconds)))
    if skip_failed is not None:
        _protected_config['skip_failed'] = bool(skip_failed)
    return protected_settings()


def protected_settings() -> dict:
    return {**_protected_config, 'cache_directory': str(PROTECTED_CACHE_DIR), 'max_segment_bytes': PROTECTED_MAX_SEGMENT_BYTES}


def _protected_cache_path(channel_id: int, token: str, suffix: str) -> Path:
    safe_suffix = suffix if suffix.startswith('.') and len(suffix) <= 12 else '.bin'
    return PROTECTED_CACHE_DIR / str(channel_id) / f'{token}{safe_suffix}'


def _protected_content_type(entry: RegistryEntry) -> str:
    suffix = entry.suffix.lower()
    return {
        '.ts': 'video/mp2t', '.m2ts': 'video/mp2t', '.mts': 'video/mp2t',
        '.m4s': 'video/iso.segment', '.mp4': 'video/mp4', '.m4a': 'audio/mp4',
        '.aac': 'audio/aac', '.mp3': 'audio/mpeg', '.vtt': 'text/vtt',
        '.webvtt': 'text/vtt', '.key': 'application/octet-stream', '.bin': 'application/octet-stream',
    }.get(suffix, 'application/octet-stream')


def _validate_protected_file(path: Path, entry: RegistryEntry, expected_length: int | None) -> int:
    size = path.stat().st_size
    if size <= 0:
        raise RuntimeError('Upstream segment completed with zero bytes')
    if size > PROTECTED_MAX_SEGMENT_BYTES:
        raise RuntimeError(f'Upstream segment exceeded the {PROTECTED_MAX_SEGMENT_BYTES // 1024 // 1024} MiB safety limit')
    if expected_length is not None and expected_length >= 0 and size != expected_length:
        raise RuntimeError(f'Upstream segment ended early ({size} of {expected_length} bytes)')
    # MPEG-TS packets are 188 bytes. A real segment can contain leading metadata or be encrypted,
    # so v0.3.4 treats sync detection as a sanity check only when obvious rather than rejecting
    # valid encrypted/provider-specific segments. The hard correctness check is complete download.
    if entry.suffix.lower() in {'.ts', '.m2ts', '.mts'} and size < 188:
        raise RuntimeError('MPEG-TS segment is too small to contain a complete transport packet')
    return size


async def _download_protected_segment(channel_id: int, token: str, entry: RegistryEntry) -> ProtectedSegmentResult:
    PROTECTED_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = _protected_cache_path(channel_id, token, entry.suffix)
    target.parent.mkdir(parents=True, exist_ok=True)
    current_url = entry.url
    retries = int(_protected_config['retries'])
    total_timeout = float(_protected_config['segment_timeout'])
    last_exc: Exception | None = None

    for attempt in range(1, retries + 1):
        tmp = target.with_name(target.name + f'.{uuid.uuid4().hex}.part')
        response: httpx.Response | None = None
        client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(connect=min(SEGMENT_CONNECT_TIMEOUT, total_timeout), read=min(SEGMENT_READ_IDLE_TIMEOUT, total_timeout), write=total_timeout, pool=min(SEGMENT_CONNECT_TIMEOUT, total_timeout)),
            headers=_segment_request_headers(),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=0),
        )
        try:
            async with asyncio.timeout(total_timeout):
                request = client.build_request('GET', current_url)
                response = await client.send(request, stream=True)
                response.raise_for_status()
                length_header = response.headers.get('content-length')
                try:
                    expected_length = int(length_header) if length_header is not None else None
                except ValueError:
                    expected_length = None
                written = 0
                with tmp.open('wb') as fh:
                    async for chunk in response.aiter_bytes(SEGMENT_CHUNK_BYTES):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > PROTECTED_MAX_SEGMENT_BYTES:
                            raise RuntimeError('Upstream segment exceeded protected-cache segment size limit')
                        fh.write(chunk)
                size = _validate_protected_file(tmp, entry, expected_length)
                os.replace(tmp, target)
                _inc('protected_downloads', channel_id)
                _inc('protected_completed', channel_id)
                _inc('protected_bytes', channel_id, size)
                if attempt > 1:
                    _event(channel_id, 'protected-recovered', f'Protected segment completed atomically on attempt {attempt}.')
                result = ProtectedSegmentResult(
                    path=target, content_type=response.headers.get('content-type') or _protected_content_type(entry),
                    size=size, attempt=attempt, final_url=str(response.url), cache_hit=False,
                )
                asyncio.create_task(_maybe_cleanup_protected_cache())
                return result
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException) as exc:
            last_exc = exc
            _inc('protected_timeouts', channel_id)
            _event(channel_id, 'protected-timeout', f'Protected segment exceeded its {total_timeout:g}s atomic-download deadline on attempt {attempt}.')
        except Exception as exc:
            last_exc = exc
            if isinstance(exc, RuntimeError):
                _inc('protected_invalid', channel_id)
        finally:
            try:
                if response is not None:
                    await response.aclose()
            finally:
                await client.aclose()
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

        if attempt < retries:
            candidate = await refresh_segment_candidate(channel_id, entry, current_url)
            if candidate:
                current_url = candidate
            _inc('protected_retries', channel_id)
            _event(channel_id, 'protected-retry', f'Retrying protected segment ({attempt + 1}/{retries}) with a fresh upstream connection.')
            await asyncio.sleep(0.10)

    _inc('protected_failures', channel_id)
    if _protected_config['skip_failed']:
        _inc('protected_skips', channel_id)
        _event(channel_id, 'protected-skip', 'Protected segment could not be acquired atomically; returning a bounded failure so the HLS client can reload/skip instead of hanging.')
    raise RuntimeError(str(last_exc or 'protected segment download failed'))


async def acquire_protected_segment(channel_id: int, token: str, entry: RegistryEntry) -> ProtectedSegmentResult:
    _inc('protected_requests', channel_id)
    target = _protected_cache_path(channel_id, token, entry.suffix)
    retention = int(_protected_config['retention_seconds'])
    try:
        age = time.time() - target.stat().st_mtime
        if target.is_file() and target.stat().st_size > 0 and age <= retention:
            _inc('protected_cache_hits', channel_id)
            return ProtectedSegmentResult(target, _protected_content_type(entry), target.stat().st_size, 0, entry.url, True)
    except FileNotFoundError:
        pass

    key = (channel_id, token)
    async with _protected_inflight_lock:
        task = _protected_inflight.get(key)
        if task is None or task.done():
            task = asyncio.create_task(_download_protected_segment(channel_id, token, entry))
            _protected_inflight[key] = task
    try:
        result = await asyncio.shield(task)
        return result
    finally:
        if task.done():
            async with _protected_inflight_lock:
                if _protected_inflight.get(key) is task:
                    _protected_inflight.pop(key, None)


async def _prefetch_one(channel_id: int, token: str) -> None:
    entry = resolve_segment_token(channel_id, token)
    if not entry or not entry.protected:
        return
    try:
        async with _protected_prefetch_semaphore:
            await acquire_protected_segment(channel_id, token, entry)
        _inc('protected_prefetches', channel_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        _inc('protected_prefetch_failures', channel_id)


def schedule_protected_prefetch(channel_id: int, tokens: list[str]) -> None:
    depth = int(_protected_config['prefetch_depth'])
    if depth <= 0 or not tokens:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for token in tokens[-depth:]:
        task = loop.create_task(_prefetch_one(channel_id, token))
        _protected_prefetch_tasks.add(task)
        task.add_done_callback(_protected_prefetch_tasks.discard)


def _cleanup_protected_cache_sync() -> None:
    global _protected_last_cleanup
    root = PROTECTED_CACHE_DIR
    if not root.exists():
        _protected_last_cleanup = time.monotonic()
        return
    now = time.time()
    retention = int(_protected_config['retention_seconds'])
    limit = int(_protected_config['cache_limit_mb']) * 1024 * 1024
    files: list[tuple[float, int, Path]] = []
    total = 0
    for path in root.rglob('*'):
        if not path.is_file():
            continue
        try:
            st = path.stat()
        except FileNotFoundError:
            continue
        if path.name.endswith('.part'):
            if now - st.st_mtime > max(30, int(_protected_config['segment_timeout']) * 2):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            continue
        if now - st.st_mtime > retention:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            continue
        files.append((st.st_mtime, st.st_size, path))
        total += st.st_size
    if total > limit:
        target = int(limit * 0.90)
        for _, size, path in sorted(files):
            if total <= target:
                break
            try:
                path.unlink()
                total -= size
            except FileNotFoundError:
                pass
    _protected_last_cleanup = time.monotonic()


async def _maybe_cleanup_protected_cache(force: bool = False) -> None:
    if not force and time.monotonic() - _protected_last_cleanup < 30:
        return
    try:
        await asyncio.to_thread(_cleanup_protected_cache_sync)
    except Exception:
        pass


def protected_segment_headers(result: ProtectedSegmentResult) -> dict[str, str]:
    return {
        'Cache-Control': 'private, max-age=30',
        'X-IPTVMM-Segment-Relay': 'protected-atomic',
        'X-IPTVMM-Protected-Cache': 'HIT' if result.cache_hit else 'MISS',
        'X-IPTVMM-Protected-Attempt': str(result.attempt),
    }


def record_variant_reresolve(channel_id: int) -> None:
    _inc('variant_reresolves', channel_id)
    _event(channel_id, 'variant-reresolve', 'Selected rendition expired; refreshed the upstream master and selected it again.')


def record_bypass(channel_id: int | None = None) -> None:
    _inc('bypasses', channel_id)


def record_failure(channel_id: int | None = None, message: str | None = None) -> None:
    _inc('failures', channel_id)
    if channel_id is not None and message:
        _event(channel_id, 'error', message)


def proxy_stats() -> dict:
    with _registry_lock:
        _trim_registry(_segment_registry, MAX_SEGMENT_TOKENS)
        _trim_registry(_playlist_registry, MAX_PLAYLIST_TOKENS)
        seg = len(_segment_registry)
        pl = len(_playlist_registry)
    return {**_stats, 'cache_entries': len(_cache), 'segment_registry_entries': seg, 'playlist_registry_entries': pl, 'relay_settings': segment_relay_settings()}


def channel_diagnostics(channel_id: int) -> dict:
    return {
        'channel_id': channel_id,
        'stats': dict(_channel_stats.get(channel_id, {})),
        'last_cdn_host': _channel_last_cdn.get(channel_id),
        'events': list(_channel_events.get(channel_id, [])),
    }


def _note_discontinuities(channel_id: int, manifest: str, base_url: str) -> None:
    lines = [x.strip() for x in manifest.replace('\r\n', '\n').replace('\r', '\n').split('\n')]
    for idx, line in enumerate(lines):
        if line.upper() != '#EXT-X-DISCONTINUITY':
            continue
        next_uri = ''
        for nxt in lines[idx + 1:]:
            if nxt and not nxt.startswith('#'):
                next_uri = urljoin(base_url, nxt)
                break
        sig = hashlib.sha1(next_uri.encode()).hexdigest()[:16] if next_uri else f'line-{idx}'
        seen = _channel_discontinuity_sigs[channel_id]
        if sig not in seen:
            seen.append(sig)
            _inc('discontinuities', channel_id)
            _event(channel_id, 'discontinuity', 'Upstream HLS discontinuity detected; compatibility rewriting remains active.')


def rewrite_media_playlist(
    manifest: str,
    base_url: str,
    channel_id: int | None = None,
    local_base: str | None = None,
    compatibility: bool = False,
    playlist_kind: str = 'media',
    relay_all: bool = False,
) -> str:
    """Rewrite playlist references while leaving ordinary media URLs direct.

    Compatibility mode aliases only extensionless/unknown media URIs. Protected mode sets
    relay_all=True, so every media segment is registered behind an atomic disk-backed local URL.
    Jellyfin never receives protected bytes until the upstream segment has fully completed.
    """
    if channel_id is not None:
        _note_discontinuities(channel_id, manifest, base_url)

    suffix = _segment_suffix(manifest, playlist_kind)
    out: list[str] = []
    newest_host = None
    media_sequence = 0
    segment_offset = 0
    protected_tokens: list[str] = []
    for raw in manifest.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        line = raw.strip()
        if not line:
            continue
        upper = line.upper()
        if line.startswith('#'):
            if upper.startswith('#EXT-X-MEDIA-SEQUENCE:'):
                media_sequence = _int_or_none(line.split(':', 1)[1].strip()) or 0
                segment_offset = 0
            media_uri_tag = upper.startswith(('#EXT-X-MAP:', '#EXT-X-PART:', '#EXT-X-PRELOAD-HINT:'))
            if media_uri_tag and 'URI=' in upper:
                tag_suffix = '.mp4' if upper.startswith('#EXT-X-MAP:') else suffix
                def media_tag_uri(m):
                    absolute = urljoin(base_url, m.group(1))
                    if compatibility and channel_id is not None and local_base and (relay_all or _needs_segment_alias(absolute)):
                        token, is_new = register_segment_url(channel_id, absolute, tag_suffix, playlist_url=base_url, protected=relay_all)
                        if relay_all:
                            protected_tokens.append(token)
                        if is_new and _needs_segment_alias(absolute):
                            _inc('extensionless_segments', channel_id)
                            _event(channel_id, 'segment-alias', f'Created a synthetic {tag_suffix} alias for an extensionless HLS media URI.')
                        return f'URI="{local_base}/hls/channel/{channel_id}/segment/{token}{tag_suffix}"'
                    return f'URI="{absolute}"'
                line = URI_ATTR_RE.sub(media_tag_uri, line)
            else:
                line = _rewrite_uri_attr_absolute(line, base_url)
            out.append(line)
            continue

        absolute = urljoin(base_url, line)
        host = urlparse(absolute).hostname
        if host:
            newest_host = host
        current_sequence = media_sequence + segment_offset
        if compatibility and channel_id is not None and local_base and (relay_all or _needs_segment_alias(absolute)):
            token, is_new = register_segment_url(
                channel_id, absolute, suffix, playlist_url=base_url, media_sequence=current_sequence, protected=relay_all,
            )
            if relay_all:
                protected_tokens.append(token)
            if is_new and _needs_segment_alias(absolute):
                _inc('extensionless_segments', channel_id)
                _event(channel_id, 'segment-alias', f'Normalized extensionless/unsupported media segment to a synthetic {suffix} URL.')
            out.append(f'{local_base}/hls/channel/{channel_id}/segment/{token}{suffix}')
        else:
            out.append(absolute)
        segment_offset += 1

    if channel_id is not None and newest_host:
        old = _channel_last_cdn.get(channel_id)
        if old and old != newest_host:
            _inc('cdn_switches', channel_id)
            _event(channel_id, 'cdn-switch', f'Upstream media CDN changed from {old} to {newest_host}.')
        _channel_last_cdn[channel_id] = newest_host
    if relay_all and channel_id is not None and protected_tokens:
        schedule_protected_prefetch(channel_id, protected_tokens)
    return '\n'.join(out) + '\n'


async def fetch_manifest(url: str) -> tuple[str, str]:
    return await _fetch_manifest(url)
