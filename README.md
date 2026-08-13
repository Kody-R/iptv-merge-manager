# IPTV Merge Manager v0.1.1

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

## Requirements

- Docker Engine
- Docker Compose plugin (`docker compose`)

## Install

```bash
unzip iptv-merge-manager-v0.1.1.zip
cd iptv-merge-manager-v0.1.1
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

v0.1.1 includes a CasaOS Compose template and an automated GitHub Container Registry workflow. See `GITHUB-CASAOS.md` for the recommended setup. Once published, CasaOS can pull the prebuilt image from GHCR rather than building the application locally.

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

Supported integer values should divide sensibly into a 24-hour day. v0.1.1 is designed around the default four-hour cycle.

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

v0.1.1 associates a selected channel with XMLTV from its own source using `tvg-id`. Only matching `<channel>` and `<programme>` records are copied into `master.xml`.

Channels without a TVG-ID can still be streamed in `master.m3u`, but they will not contribute guide entries to `master.xml` in v0.1.1.

Cross-provider manual EPG mapping is intentionally reserved for a later release.

## Security note

v0.1.1 does not include login/authentication. It is intended for a trusted home LAN. Do not expose port 8080 directly to the public Internet without placing it behind your own authenticated reverse proxy or VPN.

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
