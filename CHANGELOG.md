# Changelog

## v0.5.0 Core

Scope-simplification release:

- Returns the application to playlist/XMLTV merge and lineup management only.
- Removes HLS proxy/protected playback and FFmpeg stabilizer code from the production container.
- Retains the v0.3 low-memory worker architecture, source safety, channel editor, bulk actions, groups, numbering, EPG suggestions, resource monitor, backup/restore, and compressed XMLTV output.
- Accepts existing v0.4.x SQLite databases; additive playback columns are ignored.
- Regenerates `master.m3u` from original stored provider stream URLs on startup.
- Stream troubleshooting moves to Samsung TV Plus Stream Lab.

## v0.3.0

Resource-optimization release retained as the Core codebase foundation.

## v0.2.0

- Full channel metadata editor.
- Bulk channel operations.
- Custom groups and group-based numbering.
- EPG match suggestions.
- Expanded dashboard.
- Backup and restore.
