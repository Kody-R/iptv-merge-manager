# IPTV Merge Manager v0.5.1 Core

A lean self-hosted Docker application focused on the original IPTV Merge Manager job: combine multiple IPTV M3U/M3U8 channel lists and optional XMLTV guides into one curated master lineup and guide.


## v0.5.1 — Automatic Include All synchronization

Each playlist source can use **Manual** (default for existing sources) or **Include All (Sync)**.
In Include All mode, every successfully refreshed channel is included in the master M3U.
New channels automatically receive numbers after the highest previously assigned number,
and channels missing from the latest successful provider playlist are excluded from
both master outputs. Existing channel settings and numbers are retained so channels
can return without losing customizations. Switching an existing source to Include All
immediately includes its currently active channels and regenerates the outputs.
Switching it back to Manual stops automatic inclusion, but does not deselect the
channels that were already included.

All three outputs (`master.m3u`, `master.xml`, and `master.xml.gz`) are regenerated
after each scheduled/manual refresh. XMLTV still requires a matching TVG-ID. To avoid
misleading output on provider failures, empty or invalid feeds and >50% sudden channel
losses (for playlists with at least 20 channels) are still rejected. A legitimate
>50% provider reduction requires deliberately adjusting the safety policy; this
release does not silently disable that protection.

**Upgrade:** Keep existing `/app/data` and `/app/output` volumes. SQLite adds one
`selection_mode` column and defaults existing sources to Manual. Back up your data
folder before upgrading from v0.4/v0.5.

## v0.5.0 Core direction

This release intentionally returns IPTV Merge Manager to playlist and guide management only. Media-proxy, HLS protection, FFmpeg stabilization, stream-repair workers, and playback diagnostics are **not part of this container**. Those experiments live in the separate Samsung TV Plus Stream Lab container.

Existing v0.4.x SQLite databases can be reused. Extra playback/stabilizer columns are left in place but ignored. On startup, `master.m3u` is regenerated from each channel's original provider `stream_url`, so old `/hls/channel/...` and `/stabilized/channel/...` routes are not emitted by the Core release.

## Features

- Multiple IPTV sources
- Remote M3U/M3U8 URLs or uploaded M3U/M3U8 files
- Optional XMLTV URL or uploaded `.xml` / `.xml.gz`
- Persistent SQLite channel catalog and lineup
- Individual channel selection
- Per-source Manual / Include All (Sync) automatic channel selection
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

## Requirements

- Docker Engine
- Docker Compose plugin (`docker compose`)

## Install

```bash
unzip iptv-merge-manager-v0.5.1.zip
cd iptv-merge-manager-v0.5.1
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

v0.5.0 includes a CasaOS Compose template and an automated GitHub Container Registry workflow. See `GITHUB-CASAOS.md` for the recommended setup. Once published, CasaOS can pull the prebuilt image from GHCR rather than building the application locally.

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

Supported integer values should divide sensibly into a 24-hour day. v0.5.0 is designed around the default four-hour cycle.

## Source refresh safety

When a refresh fails, the application records the error but does not mark the existing channels from that source as removed. The last successfully imported channel catalog and cached guide remain available.

When a refresh succeeds, channels missing from the new playlist are marked inactive. They are retained in SQLite so their historical lineup state is not immediately destroyed.

New channels in **Manual** sources are imported as not selected. Channels in **Include All (Sync)** sources are selected automatically after each successful refresh.

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

v0.5.0 associates a selected channel with XMLTV from its own source using `tvg-id`. Only matching `<channel>` and `<programme>` records are copied into `master.xml`.

Channels without a TVG-ID can still be streamed in `master.m3u`, but they will not contribute guide entries to `master.xml` in v0.5.0.

Cross-provider manual EPG mapping is intentionally reserved for a later release.

## Security note

v0.5.0 does not include login/authentication. It is intended for a trusted home LAN. Do not expose port 8080 directly to the public Internet without placing it behind your own authenticated reverse proxy or VPN.

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

## v0.5.0 highlights

- Channel metadata overrides
- Bulk channel operations
- Custom lineup groups
- Group-based auto numbering
- EPG match suggestions
- Expanded dashboard
- Configuration backup and restore

## v0.5.0 low-memory architecture

v0.5.0 is designed for small CasaOS systems and large IPTV/XMLTV feeds. The long-running FastAPI process no longer parses XMLTV or retains provider payloads. Heavy refresh, EPG, and output work runs in a short-lived worker process, which returns its memory to the operating system when the job ends.

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
