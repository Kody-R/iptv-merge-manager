from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx

APP_VERSION = '0.3.1'
DEFAULT_MAX_HEIGHT = 720
DEFAULT_CACHE_SECONDS = 15
MAX_MANIFEST_BYTES = 512 * 1024
DEFAULT_USER_AGENT = os.getenv(
    'HLS_PROXY_USER_AGENT',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
)

ATTR_RE = re.compile(r'([A-Z0-9-]+)=(?:"([^"]*)"|([^,]*))', re.I)
URI_ATTR_RE = re.compile(r'URI="([^"]+)"', re.I)


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


_cache: dict[str, CacheEntry] = {}
_cache_lock = asyncio.Lock()
_stats = {
    'requests': 0,
    'master_resolves': 0,
    'cache_hits': 0,
    'bypasses': 0,
    'failures': 0,
}


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
    """Return the variants from an HLS master playlist without loading media segments."""
    lines = [line.strip() for line in manifest.replace('\r\n', '\n').replace('\r', '\n').split('\n')]
    variants: list[Variant] = []
    for idx, line in enumerate(lines):
        if not line.upper().startswith('#EXT-X-STREAM-INF:'):
            continue
        attrs = _parse_attrs(line)
        uri = None
        for nxt in lines[idx + 1:]:
            if not nxt:
                continue
            if nxt.startswith('#'):
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
        # Nothing fits below the cap. Use the smallest available rendition rather than failing playback.
        return min(with_height, key=lambda v: (v.height or 10**9, v.bandwidth or 10**12))

    return max(variants, key=lambda v: (v.height or 0, v.bandwidth or 0, v.width or 0))


def _rewrite_uri_attr(line: str, base_url: str) -> str:
    return URI_ATTR_RE.sub(lambda m: f'URI="{urljoin(base_url, m.group(1))}"', line)


def build_locked_master(manifest: str, base_url: str, selected: Variant) -> str:
    """Create a one-variant master. Media segments remain direct from the upstream CDN."""
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
        if (media_type, group_id) in referenced_groups:
            media_lines.append(_rewrite_uri_attr(line, base_url))

    out = ['#EXTM3U']
    out.extend(version_lines[:1])
    out.append(
        f'# IPTV Merge Manager v{APP_VERSION} variant lock: '
        f'{selected.width or "?"}x{selected.height or "?"} @ {selected.bandwidth or "?"} bps'
    )
    out.extend(media_lines)
    out.append(selected.info_line)
    out.append(selected.absolute_uri)
    return '\n'.join(out) + '\n'


def is_http_url(url: str) -> bool:
    return urlparse(url).scheme.lower() in {'http', 'https'}


async def _fetch_manifest(url: str) -> tuple[str, str]:
    if not is_http_url(url):
        raise ValueError('HLS variant proxy supports HTTP/HTTPS streams only')

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
                    # Some redirect/resolver services return a generic content type. Peek at only
                    # the first chunk so an explicitly enabled non-HLS live stream is not consumed.
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
            _stats['cache_hits'] += 1
            return cached

    manifest, final_url = await _fetch_manifest(url)
    variants = parse_master(manifest, final_url) if manifest else []
    entry = CacheEntry(
        expires_at=now + max(1, cache_seconds),
        manifest=manifest,
        final_url=final_url,
        variants=variants,
    )
    async with _cache_lock:
        _cache[url] = entry
    _stats['master_resolves'] += 1
    return entry


async def invalidate(url: str) -> None:
    async with _cache_lock:
        _cache.pop(url, None)


def record_request() -> None:
    _stats['requests'] += 1


def record_bypass() -> None:
    _stats['bypasses'] += 1


def record_failure() -> None:
    _stats['failures'] += 1


def proxy_stats() -> dict:
    return {**_stats, 'cache_entries': len(_cache)}


def rewrite_media_playlist(manifest: str, base_url: str) -> str:
    """Make all media/segment/key URIs absolute so Jellyfin fetches them directly upstream."""
    out: list[str] = []
    for raw in manifest.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        line = raw.strip()
        if not line:
            continue
        if line.startswith('#'):
            out.append(_rewrite_uri_attr(line, base_url))
        else:
            out.append(urljoin(base_url, line))
    return '\n'.join(out) + '\n'


async def fetch_manifest(url: str) -> tuple[str, str]:
    """Public small-manifest fetch used for the selected media playlist."""
    return await _fetch_manifest(url)
