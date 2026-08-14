# Changelog

## v0.3.4

Protected Playback reliability release:

- Added a new Protected HLS mode that locks the selected rendition and routes every media segment through IPTV Merge Manager.
- Protected segments are downloaded completely to a temporary disk file before Jellyfin receives any bytes.
- Added atomic rename after successful whole-segment completion so partial upstream responses are never published.
- Added whole-segment deadlines, fresh-connection retries, same-media-sequence URL recovery, and bounded failure/skip responses.
- Added two-segment prefetch by default and single-flight request de-duplication for concurrent Jellyfin requests.
- Added disk-backed completed-segment reuse with 512 MB default cache ceiling and 180-second default retention.
- Added cleanup of stale cache data and abandoned temporary segment files.
- Added Protected download/cache-hit/prefetch/timeout/retry/failure/skip/byte diagnostics.
- Added UI controls for Protected prefetch depth, deadline, attempts, skip behavior, cache size, and retention.
- Existing v0.3.3 Fixed channel/source/global selections migrate once to Protected on upgrade.
- Kept Direct, Compatibility, and Fixed modes available.
- Preserved one Uvicorn worker and disk-backed XMLTV/refresh architecture; IPTV Merge Manager still does not transcode media.

## v0.3.3

Guarded HLS segment resilience patch:

- Replaced v0.3.2 synthetic-segment HTTP redirects with a guarded streaming relay only for extensionless/unsupported compatibility aliases.
- Ordinary recognized HLS media URLs remain direct from Jellyfin to the provider/CDN.
- Added bounded connect/first-byte/read-idle/overall segment deadlines.
- Added bounded retries before any media bytes are emitted.
- Before a pre-media retry, refreshes the same media playlist and can follow a changed URL only for the exact same HLS media sequence.
- Added a mid-segment stale-data watchdog; partial segments are never restarted/concatenated.
- Added downstream-disconnect cancellation and guaranteed upstream response/client cleanup.
- Added relay, retry, timeout, cancellation, completion, byte, and failure diagnostics.
- Added Compose/CasaOS environment controls for relay timeouts and attempt count.
- Preserved the v0.3 low-memory one-worker, disk-backed refresh/XMLTV architecture.

## v0.3.2

HLS compatibility release:

- Added Direct, Playlist Compatibility, and Fixed Variant + Compatibility modes.
- Added Global → Source → Channel HLS inheritance.
- Normalized extensionless/unsupported HLS media URLs to short-lived synthetic safe-extension aliases.
- Added child-playlist routing and discontinuity/CDN-change diagnostics.
- Preserved direct media delivery for recognized `.ts`/`.m4s` segments.
- Migrated v0.3.1 Variant Lock settings without changing expected channel behavior.

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
