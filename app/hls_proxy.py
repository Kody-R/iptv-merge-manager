from __future__ import annotations

import asyncio
import hashlib
import os
import re
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx

APP_VERSION = '0.3.2'
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

VALID_HLS_MODES = {'direct', 'compat', 'fixed'}
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
    'segment_redirects': 0,
    'extensionless_segments': 0,
    'discontinuities': 0,
    'cdn_switches': 0,
    'variant_reresolves': 0,
}
_channel_stats: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
_channel_events: dict[int, deque[dict]] = defaultdict(lambda: deque(maxlen=EVENT_HISTORY_LIMIT))
_channel_last_cdn: dict[int, str] = {}
_channel_discontinuity_sigs: dict[int, deque[str]] = defaultdict(lambda: deque(maxlen=32))


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


def register_segment_url(channel_id: int, url: str, suffix: str = '.ts') -> tuple[str, bool]:
    if not is_http_url(url):
        raise ValueError('Only HTTP/HTTPS segment URLs can be registered')
    token = _token(channel_id, url, 'segment')
    now = time.monotonic()
    with _registry_lock:
        _trim_registry(_segment_registry, MAX_SEGMENT_TOKENS)
        is_new = token not in _segment_registry
        _segment_registry[token] = RegistryEntry(url, channel_id, now + SEGMENT_TOKEN_TTL, suffix=suffix)
    return token, is_new


def resolve_segment_token(channel_id: int, token: str) -> RegistryEntry | None:
    with _registry_lock:
        _trim_registry(_segment_registry, MAX_SEGMENT_TOKENS)
        entry = _segment_registry.get(token)
        if not entry or entry.channel_id != channel_id:
            return None
        return entry


def register_playlist_url(channel_id: int, url: str, kind: str = 'media') -> str:
    if not is_http_url(url):
        raise ValueError('Only HTTP/HTTPS playlist URLs can be registered')
    token = _token(channel_id, url, 'playlist')
    now = time.monotonic()
    with _registry_lock:
        _trim_registry(_playlist_registry, MAX_PLAYLIST_TOKENS)
        _playlist_registry[token] = RegistryEntry(url, channel_id, now + PLAYLIST_TOKEN_TTL, kind=kind, suffix='.m3u8')
    return token


def resolve_playlist_token(channel_id: int, token: str) -> RegistryEntry | None:
    with _registry_lock:
        _trim_registry(_playlist_registry, MAX_PLAYLIST_TOKENS)
        entry = _playlist_registry.get(token)
        if not entry or entry.channel_id != channel_id:
            return None
        return entry


def _local_playlist_url(local_base: str, channel_id: int, upstream: str, kind: str = 'media') -> str:
    token = register_playlist_url(channel_id, upstream, kind)
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


def build_compat_master(manifest: str, base_url: str, channel_id: int, local_base: str) -> str:
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
                    lambda m: f'URI="{_local_playlist_url(local_base, channel_id, urljoin(base_url, m.group(1)), kind)}"',
                    line,
                )
            elif upper.startswith('#EXT-X-I-FRAME-STREAM-INF:') and 'URI=' in upper:
                line = URI_ATTR_RE.sub(
                    lambda m: f'URI="{_local_playlist_url(local_base, channel_id, urljoin(base_url, m.group(1)), "media")}"',
                    line,
                )
            out.append(line)
            continue
        if expect_variant:
            out.append(_local_playlist_url(local_base, channel_id, urljoin(base_url, line), 'media'))
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
                lambda m: f'URI="{_local_playlist_url(local_base, channel_id, urljoin(base_url, m.group(1)), kind)}"',
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
        out.append(_local_playlist_url(local_base, channel_id, selected.absolute_uri, 'media'))
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
    _inc('segment_redirects', channel_id)


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
    return {**_stats, 'cache_entries': len(_cache), 'segment_registry_entries': seg, 'playlist_registry_entries': pl}


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
) -> str:
    """Rewrite playlist references without relaying media bytes.

    In compatibility mode, extensionless/unknown media URIs are replaced with short-lived local
    URLs carrying a safe synthetic extension. The local endpoint only redirects to the provider.
    """
    if channel_id is not None:
        _note_discontinuities(channel_id, manifest, base_url)

    suffix = _segment_suffix(manifest, playlist_kind)
    out: list[str] = []
    newest_host = None
    for raw in manifest.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        line = raw.strip()
        if not line:
            continue
        upper = line.upper()
        if line.startswith('#'):
            media_uri_tag = upper.startswith(('#EXT-X-MAP:', '#EXT-X-PART:', '#EXT-X-PRELOAD-HINT:'))
            if media_uri_tag and 'URI=' in upper:
                tag_suffix = '.mp4' if upper.startswith('#EXT-X-MAP:') else suffix
                def media_tag_uri(m):
                    absolute = urljoin(base_url, m.group(1))
                    if compatibility and channel_id is not None and local_base and _needs_segment_alias(absolute):
                        token, is_new = register_segment_url(channel_id, absolute, tag_suffix)
                        if is_new:
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
        if compatibility and channel_id is not None and local_base and _needs_segment_alias(absolute):
            token, is_new = register_segment_url(channel_id, absolute, suffix)
            if is_new:
                _inc('extensionless_segments', channel_id)
                _event(channel_id, 'segment-alias', f'Normalized extensionless/unsupported media segment to a synthetic {suffix} URL.')
            out.append(f'{local_base}/hls/channel/{channel_id}/segment/{token}{suffix}')
        else:
            out.append(absolute)

    if channel_id is not None and newest_host:
        old = _channel_last_cdn.get(channel_id)
        if old and old != newest_host:
            _inc('cdn_switches', channel_id)
            _event(channel_id, 'cdn-switch', f'Upstream media CDN changed from {old} to {newest_host}.')
        _channel_last_cdn[channel_id] = newest_host
    return '\n'.join(out) + '\n'


async def fetch_manifest(url: str) -> tuple[str, str]:
    return await _fetch_manifest(url)
