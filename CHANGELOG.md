# Changelog

## v0.3.2

HLS compatibility and resilience release:

- Added Direct, Playlist Compatibility, and Fixed Variant + Compatibility modes.
- Added global → source → channel HLS setting inheritance.
- Migrates v0.3.1 locked channels to Fixed Variant + Compatibility and unlocked channels to Direct.
- Detects extensionless/unsupported media-segment URLs and exposes short-lived local aliases with safe synthetic extensions.
- Segment alias endpoints issue redirects only; IPTV Merge Manager does not buffer or relay media payloads.
- Added bounded, expiring segment and child-playlist registries to prevent unbounded memory growth.
- Adaptive Compatibility mode rewrites child playlist references back through the local playlist layer while preserving the master variants.
- Preserves HLS discontinuity metadata and records new discontinuities in per-channel diagnostics.
- Detects upstream CDN host changes and records them as HLS events.
- Retains 401/403/404/410 master re-resolution and adds a visible re-resolve counter.
- Added per-channel HLS diagnostics with a bounded recent-event history.
- Expanded global HLS runtime counters for segment redirects, extensionless fixes, discontinuities, CDN changes, and variant re-resolves.
- Keeps normal `.ts`/`.m4s` media URLs direct to the provider/CDN to preserve v0.3 low-resource behavior.

## v0.3.1

Adaptive-HLS playback reliability patch:

- Added opt-in per-channel HLS Variant Lock for Jellyfin/FFmpeg playback issues.
- Default variant cap is 720p, with Highest/1080p/720p/540p/360p controls.
- Added HLS master analyzer in the channel editor.
- Added bulk enable/disable and quality-cap actions.
- Resolves dynamic provider master playlists at playback time instead of storing expiring rendition URLs.
- For multiplexed A/V variants, serves the selected media playlist directly so FFmpeg sees only one program.
- Rewrites segment/key URIs to absolute upstream URLs; video traffic does not traverse IPTV Merge Manager.
- Preserves separate HLS audio groups using a one-variant mini-master when necessary.
- Retries master resolution once when a selected rendition returns 401/403/404/410.
- Adds a short configurable manifest cache (15 seconds by default) and proxy runtime counters.
- `master.m3u` expands internal proxy URLs from the request host, avoiding a hard-coded server IP.
- Preserves all v0.3.0 low-memory worker, disk-backed XMLTV, pagination, and resource-profile behavior.

## v0.3.0

Resource-optimization release:

- Short-lived worker process for refresh, XMLTV parsing, EPG suggestions, and output generation.
- Streaming HTTP downloads and uploaded-file handling.
- Streaming M3U parser; no full-playlist string/list retained in RAM.
- Streaming XMLTV `iterparse` with sibling cleanup.
- Disk-backed guide cache plus SQLite-only EPG channel index.
- Sequential source refreshes.
- Paginated channel browser with Low Memory/Balanced/Performance profiles.
- Resource Monitor for web RSS, container RAM, worker refresh peak, disk cache, and outputs.
- Refresh-history retention limits.
- Atomic temporary-file generation for M3U/XMLTV.
- New compressed `/output/master.xml.gz` output.
- Last-known-good protection against empty/invalid feeds and sudden >50% lineup/EPG loss.
- Single explicit Uvicorn worker and lightweight health check.
- Preserves v0.2 channel editor, bulk actions, custom groups, numbering, EPG suggestions, dashboard, and backup/restore.

## v0.2.0

- Full channel metadata editor.
- Bulk channel operations.
- Custom groups and group-based numbering.
- EPG match suggestions.
- Expanded dashboard.
- Backup and restore.
