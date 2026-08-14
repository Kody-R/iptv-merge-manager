# IPTV Merge Manager v0.3.3

A self-hosted Docker application that combines multiple IPTV M3U/M3U8 channel lists and optional XMLTV guides into one curated master lineup.

## Features

- Multiple IPTV sources
- Remote M3U/M3U8 URLs or uploaded M3U/M3U8 files
- Optional XMLTV URL or uploaded `.xml` / `.xml.gz`
- Persistent SQLite channel catalog and lineup
- Individual channel selection
- Search, source filtering, and group filtering
- Drag-and-drop master lineup ordering
- Manual channel numbers
- Automatic sequential numbering
- Automatic fill-blank/gap numbering
- Duplicate channel-number warning
- Four-hour automatic refresh schedule
- Manual Refresh All and per-source refresh
- Last-known-good behavior when a provider refresh fails
- Detection of new, changed, and removed upstream channels
- Generated `/output/master.m3u`
- Generated `/output/master.xml`
- XMLTV output filtered to selected TVG IDs
- Refresh history in the web UI


## v0.3.3 Guarded HLS Segment Resilience

v0.3.3 keeps the v0.3.2 HLS compatibility modes and adds a selective safety relay for the exceptional media URLs that v0.3.2 normalized to synthetic `.ts`/`.m4s`/audio aliases. This specifically targets providers that hang on commercial/ad-break segments instead of returning an error.

Normal media traffic is still direct:

```text
ordinary .ts/.m4s segment -> Jellyfin fetches provider/CDN directly
extensionless/unsupported compatibility segment -> IPTVMM guarded relay -> provider/CDN
```

The guarded relay:

- acquires the first media bytes before committing a response to Jellyfin;
- retries a pre-media failure a bounded number of times;
- refreshes the same upstream media playlist on a pre-media failure and follows a replacement URL only when it still represents the exact same HLS media sequence;
- aborts a segment that stops producing bytes;
- enforces an overall per-attempt deadline;
- cancels the upstream HTTP request when the downstream request disappears;
- never restarts a segment after partial bytes were already emitted, avoiding concatenated/corrupt media;
- adds relay/retry/timeout/cancellation diagnostics to the dashboard and per-channel HLS diagnostics.

Default safeguards are configurable through environment variables:

```yaml
HLS_SEGMENT_CONNECT_TIMEOUT: "4"
HLS_SEGMENT_FIRST_BYTE_TIMEOUT: "6"
HLS_SEGMENT_READ_IDLE_TIMEOUT: "10"
HLS_SEGMENT_TOTAL_TIMEOUT: "20"
HLS_SEGMENT_PLAYLIST_REFRESH_TIMEOUT: "4"
HLS_SEGMENT_MAX_ATTEMPTS: "2"
```

These controls apply only to synthetic compatibility segment aliases. They do not turn IPTV Merge Manager into a full IPTV proxy or transcoder.

## v0.3.1 Adaptive HLS Variant Lock

v0.3.1 adds an opt-in playback-reliability path for adaptive HLS channels that expose multiple quality renditions and behave poorly when Jellyfin/FFmpeg opens the full master playlist.

For an enabled channel, IPTV Merge Manager now:

1. Keeps the provider's stable/original stream URL in SQLite.
2. Resolves the current upstream HLS master when playback starts.
3. Selects one rendition (720p maximum by default).
4. Fetches only that rendition's small media playlist.
5. Rewrites relative segment/key URIs to absolute upstream CDN URLs.
6. Returns the rewritten playlist to Jellyfin.
7. Leaves all `.ts`/media traffic direct between Jellyfin and the provider/CDN.

This prevents Jellyfin from seeing several video/audio programs at the same time while keeping IPTV Merge Manager's CPU, RAM, and network load very small. If a master uses a separate HLS audio group, v0.3.1 returns a one-variant mini-master so that audio group is retained.

### Enable it for a problem channel

Open **Channel Browser → Edit** for the channel, then:

- Check **Enable HLS Variant Lock for this channel**.
- Leave **Maximum HLS quality** at **Use global default** (720p initially), or choose another cap.
- Click **Analyze HLS** to verify available variants and which one will be selected.
- Save the channel.

You can also enable/disable variant lock or assign 720p/540p/360p caps to multiple checked channels using the Channel Browser bulk-action menu.

The **Adaptive HLS Variant Lock** dashboard panel controls the global service, default quality cap, short master-manifest cache, and runtime request/error counters.

### Important behavior

Variant lock is **off per channel by default** after upgrading. Existing streams remain unchanged until you enable it for a channel. This avoids adding an HLS probe to every IPTV stream in a large lineup.

The generated `master.m3u` uses the same host/port Jellyfin used to request the playlist when expanding internal variant-lock URLs, so normal direct-IP and CasaOS LAN usage do not require a hard-coded server address.

## Requirements

- Docker Engine
- Docker Compose plugin (`docker compose`)

## Install

```bash
unzip iptv-merge-manager-v0.3.3.zip
cd iptv-merge-manager-v0.3.3
docker compose up -d --build
```

Open:

```text
http://YOUR-SERVER-IP:8080/
```

Generated feeds:

```text
http://YOUR-SERVER-IP:8080/output/master.m3u
http://YOUR-SERVER-IP:8080/output/master.xml
```

For Jellyfin, add the M3U URL as an M3U tuner and the XML URL as an XMLTV guide provider.


## CasaOS / GitHub deployment

v0.3.0 includes a CasaOS Compose template and an automated GitHub Container Registry workflow. See `GITHUB-CASAOS.md` for the recommended setup. Once published, CasaOS can pull the prebuilt image from GHCR rather than building the application locally.

Files added for this workflow:

```text
casaos/docker-compose.yml
casaos/icon.svg
.github/workflows/docker-publish.yml
GITHUB-CASAOS.md
```

The GitHub Actions workflow builds both `linux/amd64` and `linux/arm64`.

## Data persistence

The Compose file mounts:

```text
./data   -> /app/data
./output -> /app/output
```

Important files:

```text
data/iptv.db          SQLite configuration/database
data/cache/            Last successful fetched M3U/XML files
data/uploads/          Uploaded source files
output/master.m3u      Generated master playlist
output/master.xml      Generated filtered XMLTV guide
```

Your selection, sort order, and assigned channel numbers live in SQLite and are not replaced when upstream playlists refresh.

## Refresh behavior

By default the container refreshes on a four-hour wall-clock schedule at hours divisible by four in the configured timezone. With the supplied Compose configuration and `America/New_York`, that is approximately:

```text
00:00
04:00
08:00
12:00
16:00
20:00
```

Change the interval with:

```yaml
environment:
  REFRESH_HOURS: "4"
```

Supported integer values should divide sensibly into a 24-hour day. v0.3.0 is designed around the default four-hour cycle.

## Source refresh safety

When a refresh fails, the application records the error but does not mark the existing channels from that source as removed. The last successfully imported channel catalog and cached guide remain available.

When a refresh succeeds, channels missing from the new playlist are marked inactive. They are retained in SQLite so their historical lineup state is not immediately destroyed.

New channels are imported as **not selected**. They will not silently appear in your master lineup.

## Automatic numbering

The Master Lineup includes two modes:

### Renumber all

Assigns numbers in lineup order using the configured start and increment.

Example: start `100`, increment `10` gives `100, 110, 120...`.

### Fill blanks only

Keeps existing numbers and assigns numbers only to selected channels with no number. Existing numbers are skipped.

## M3U notes

The input needs to be an IPTV-style extended M3U containing `#EXTINF` channel entries. A raw single-channel HLS media manifest containing only HLS segments is not treated as a channel lineup.

The generated M3U includes `tvg-id`, `tvg-name`, `tvg-logo`, `group-title`, and `tvg-chno` when available.

## XMLTV behavior

v0.3.0 associates a selected channel with XMLTV from its own source using `tvg-id`. Only matching `<channel>` and `<programme>` records are copied into `master.xml`.

Channels without a TVG-ID can still be streamed in `master.m3u`, but they will not contribute guide entries to `master.xml` in v0.3.0.

Cross-provider manual EPG mapping is intentionally reserved for a later release.

## Security note

v0.3.0 does not include login/authentication. It is intended for a trusted home LAN. Do not expose port 8080 directly to the public Internet without placing it behind your own authenticated reverse proxy or VPN.

Also note that source URLs are stored in the local SQLite database. If a provider embeds credentials/tokens in its URL, protect the `data` directory accordingly.

## Useful commands

Start/build:

```bash
docker compose up -d --build
```

View logs:

```bash
docker compose logs -f
```

Restart:

```bash
docker compose restart
```

Stop:

```bash
docker compose down
```

Update after replacing application files:

```bash
docker compose up -d --build
```

The persistent `data` and `output` folders remain on the host.

## v0.3.3 highlights

- Selective guarded relay for extensionless/unsupported HLS compatibility segments.
- 4-second connect, 6-second first-media, 10-second read-idle, and 20-second per-attempt safeguards by default.
- Maximum two pre-media attempts by default.
- Mid-segment stale-data watchdog closes the segment instead of leaving FFmpeg blocked indefinitely.
- Downstream disconnect cancellation closes the corresponding upstream HTTP request.
- Same-media-sequence URL recovery after a failed ad/CDN segment when the refreshed playlist republishes that sequence at a new URL.
- New relay/retry/timeout/cancellation/recovery runtime counters and channel events.
- Ordinary `.ts`, `.m4s`, audio, key, and other recognized media URLs remain direct to the provider/CDN.
- No FFmpeg, transcoding, or full-stream media proxy was added to IPTV Merge Manager.

## v0.3.1 highlights

- Channel metadata overrides
- Bulk channel operations
- Custom lineup groups
- Group-based auto numbering
- EPG match suggestions
- Expanded dashboard
- Configuration backup and restore
- Per-channel adaptive HLS variant lock
- 720p default rendition cap with per-channel overrides
- HLS analyzer and runtime proxy counters
- Dynamic provider-manifest re-resolution
- Direct-to-CDN media segment delivery

## v0.3.0 low-memory architecture

v0.3.0 is designed for small CasaOS systems and large IPTV/XMLTV feeds. The long-running FastAPI process no longer parses XMLTV or retains provider payloads. Heavy refresh, EPG, and output work runs in a short-lived worker process, which returns its memory to the operating system when the job ends.

Resource changes include:

- Streaming HTTP downloads in 256 KiB chunks directly to disk.
- Streaming M3U parsing instead of reading the whole playlist into memory.
- `lxml.iterparse()` XMLTV processing with aggressive element cleanup.
- Disk-backed raw XMLTV cache; programme records are not stored in RAM or SQLite.
- A compact SQLite EPG channel index used by the dashboard and EPG suggestions.
- Sequential source refreshes so multiple large guides never overlap in memory.
- Short-lived worker isolation for XMLTV parsing and output generation.
- Backend channel pagination (100 rows in Low Memory mode).
- Resource profiles: Low Memory, Balanced, and Performance.
- Resource Monitor showing web RSS, container usage/limit, last refresh peak, cache size, and output size.
- Atomic `.tmp` output generation and automatic `master.xml.gz` output.
- Last-known-good source protection for empty/invalid feeds and sudden >50% channel loss.
- Configurable refresh-history retention via resource profile.
- Single Uvicorn worker and lightweight `/health` container health check.

### Resource profiles

| Profile | Channel page size | Refresh history | Refresh concurrency |
|---|---:|---:|---:|
| Low Memory | 100 | 10 | 1 |
| Balanced | 250 | 30 | 1 |
| Performance | 500 | 100 | 1 |

The refresh worker's peak RAM depends on provider guide size and XML structure, so specific RAM usage cannot be guaranteed. The architecture is intended to keep idle memory substantially below v0.2 because large XML allocations no longer live in the web-server process.
