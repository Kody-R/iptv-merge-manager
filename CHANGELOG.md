## v0.3.1 — Automatic source synchronization

- Per-source Manual or Include All (Sync) mode.
- On successful refresh, new channels are included automatically and numbered without altering existing channel numbers.
- Channels missing upstream are removed from generated output via the active flag; they reappear if the provider restores them.
- Switching an existing source to Include All immediately includes active channels and regenerates output.
- Failed/suspicious refreshes preserve last-known-good data.
- Existing sources retain Manual mode by default on upgrade.

# Changelog

## v0.3.1

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
