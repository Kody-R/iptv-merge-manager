# Upgrade to v0.5.1 from v0.5.0 Core

**What changed:** per-source automatic Include All (Sync) selection. The rest of v0.5.0 Core's streaming/low-memory architecture stays in place.

1. Back up `/DATA/AppData/iptv-merge-manager/data` (at least `iptv.db` and uploaded playlists) before redeploying.
2. Replace the source code in your repository with the contents of the v0.5.1 ZIP; do not overwrite persistent CasaOS `data` or `output` volumes.
3. Commit and push, create and push tag `v0.5.1`.
4. Let GitHub Actions publish `ghcr.io/<your-owner>/iptv-merge-manager:0.5.1`, then change the CasaOS Compose image to that tag and redeploy.
5. Open **Sources**, find the changing playlist, and select **Include All (Sync)**. This immediately includes current active channels, assigns unused channel numbers where necessary, and regenerates the master feeds.
6. Keep other curated sources in **Manual** mode. Existing sources remain Manual on upgrade.

## How syncing works

- Successful refresh: previously known channel details are updated, new channels added/selected/numbered, disappeared channels marked inactive, and master feeds regenerated.
- Returning channels recover their old row, number and custom metadata when their provider stable identifier is unchanged.
- Switching from Include All back to Manual stops automatic inclusion of future new channels but leaves existing selections intact.
- In Include All mode, manually unchecking one channel will be overridden by the **next successful refresh**. Use Manual mode for individual exclusions.
- The app still rejects empty input and sudden >50% channel loss (when the last playlist had at least 20 channels) as a last-known-good safeguard. Legitimate very large provider reductions require separate administrative handling; this release does not silently bypass safety checks.
- `master.m3u`, `master.xml` and `master.xml.gz` update after the application refresh. Jellyfin reads these only on its own tuner/guide refresh schedule; this app does not trigger Jellyfin refreshes.

## Feed URLs

`http://YOUR-SERVER:8080/output/master.m3u`

`http://YOUR-SERVER:8080/output/master.xml`

`http://YOUR-SERVER:8080/output/master.xml.gz`

Use the normal `.xml` URL for Jellyfin unless you have tested gzip support in your particular integration.
