# Changelog

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
