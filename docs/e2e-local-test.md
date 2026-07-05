# Local E2E Test Guide

> Development preview: Screenarr is an experimental work-in-progress bridge for
> early testing and collaboration. It is not a supported public release yet, and
> breaking changes should be expected.

This guide is for testing a full OnScreen request flow through Screenarr,
with MediaManager coordinating Prowlarr and qBittorrent, using public-domain
media fixtures.

Happy path:

```text
OnScreen request
-> Screenarr Arr-compatible service
-> MediaManager
-> Prowlarr
-> qBittorrent
-> MediaManager import
-> OnScreen library scan marks the request available
```

Direct Internet Archive torrent use is diagnostic fallback only. Use it to
prove whether a public-domain payload is alive, seeded, and importable when the
Prowlarr/MediaManager path is unclear. Do not count direct IA, qBittorrent, or
Prowlarr operations as Screenarr features.

## OnScreen Arr Services

Create two OnScreen Arr-service entries that point to the same Screenarr URL
and API key:

| Name | Kind | Use | URL |
| --- | --- | --- | --- |
| Screenarr Radarr | Radarr | Movies | `http://screenarr:7879` |
| Screenarr Sonarr | Sonarr | Shows | `http://screenarr:7879` |

Both entries use the same API key value as `BRIDGE_API_KEY`.

## Public-Domain Fixtures

Run all three lanes when validating a real-world build:

| Lane | Request in OnScreen | IA diagnostic fixture | Purpose |
| --- | --- | --- | --- |
| Movie / Radarr | `Night of the Living Dead` | `https://archive.org/details/Night.Of.The.Living.Dead_1080p` | Clean movie request, Radarr kind, movie import, availability scan. |
| Clean TV / Sonarr | `The Lucy Show` S05E01 / 5x01 | `https://archive.org/details/TheLucyShow5x0191266LucyAndGeorgeBurns` | Clean show request with normal season/episode parsing. |
| Messy TV / Sonarr | `The Beverly Hillbillies` S01E01 / Ep01 `The Clampetts Strike Oil` | `https://archive.org/details/Beverly_Hillbillies_Ep01_The_Clampetts_Strike_Oil` | Messy release naming, episode title matching, and import parser behavior. |

## Clean Pass

1. Start OnScreen, Screenarr, MediaManager, Prowlarr, and qBittorrent.
1. Confirm OnScreen has both `Screenarr Radarr` and `Screenarr Sonarr`
   configured with the same Screenarr URL and API key.
1. Confirm Screenarr health:

```bash
curl http://localhost:7879/healthz
```

1. Confirm the bridge queue is reachable:

```bash
curl -H "X-Api-Key: $BRIDGE_API_KEY" \
  http://localhost:7879/api/bridge/v1/queue
```

1. In OnScreen, request the fixture for one lane.
1. In Screenarr, confirm manual mode waits in `needs_release`, auto mode moves
   to `download_submitted`, and ambiguous submits land in `download_unverified`
   instead of silently retrying.
1. In MediaManager, confirm the item exists in the expected library and has
   release/search/import state.
1. In Prowlarr, confirm searches return candidates and are not rate limited.
1. In qBittorrent, confirm the selected release downloads or records a clear
   stalled/dead-torrent state.
1. In MediaManager, confirm the item imports into the media folder with the
   expected movie or show path.
1. In Screenarr, reconcile the queue item if it is still `download_submitted`
   or `download_unverified`.
1. In OnScreen, scan the library and confirm the request becomes available.

## Expected Screenarr States

- `needs_release`: waiting for an operator to pick a candidate.
- `download_submitted`: MediaManager accepted the selected candidate.
- `download_unverified`: Screenarr submitted the grab but cannot prove the
  MediaManager outcome; reconcile before retrying to avoid duplicate grabs.
- `download_failed`: MediaManager returned a clear failure. Retry after fixing
  transient upstream issues; fix invalid request/configuration errors first.
- `imported`: MediaManager reports the item downloaded or imported.
- `available`: OnScreen later confirms the request is available.

## Evidence Checklist

Capture enough evidence to explain where the request is, without saving secret
material.

- Screenarr queue row and queue events. Redact API keys, cookies, signed webhook
  headers, dashboard sessions, and request bodies if they contain secrets.
- MediaManager item, selected release, download status, and import status.
  Redact MediaManager tokens, usernames, passwords, and cookies.
- Prowlarr search and candidate evidence for the fixture. Redact indexer API
  keys, auth headers, cookies, and private tracker details.
- qBittorrent torrent status, progress, peers/seeds if useful, and stalled
  reason. Redact WebUI credentials and private tracker URLs.
- Imported file path confirmation. Prefer a library-root-relative path when
  sharing evidence.
- OnScreen availability proof from the browser, API, or logs. Redact user
  tokens, cookies, TMDB keys, webhook secrets, and signed payload material.

Do not store secret-bearing logs, screenshots, browser HAR files, database
dumps, or raw config files as E2E artifacts.

## Timeout And Failure Accounting

| Owner | Symptom | Account for it as |
| --- | --- | --- |
| Prowlarr | `429`, rate limit, or empty candidate refresh after repeated searches | Upstream indexer throttling. Wait for cooldown, then refresh candidates. |
| Public-domain torrent | No seeds, very slow peers, or partial availability | Dead/slow fixture. Try later or record it as a fixture failure. |
| qBittorrent | Main media file completes but small IA sidecar files stall | Download-client/payload edge case. Verify importable media; do not call it a Screenarr feature. |
| MediaManager | Submit times out or returns ambiguous state | Screenarr should show `download_unverified`; reconcile before retrying. |
| MediaManager import | File lands with an unexpected name or episode parse fails | Import naming/parsing mismatch. Capture candidate name, final path, and parser result. |
| OnScreen | Metadata lookup, TMDB match, scan, or availability state fails | OnScreen metadata or library availability issue. Capture browser/API/log proof. |

## Diagnostic Fallback

If the public-domain torrent stalls on small Internet Archive sidecar pieces,
verify whether the movie file itself completed and can be imported by
MediaManager. Do not add direct Internet Archive or qBittorrent behavior to
Screenarr for this; the bridge should stay between OnScreen and MediaManager.

Use direct IA torrent checks only after the happy path is blocked and only to
answer a narrow question: is the public-domain payload available enough for
MediaManager to import? Return to the OnScreen -> Screenarr -> MediaManager ->
Prowlarr -> qBittorrent lane for any pass/fail E2E claim.

## Cleanup

1. Remove test requests from OnScreen if they were created only for this run.
1. Clear completed or failed Screenarr test queue items through the dashboard or
   queue API when that is available.
1. Remove test torrents from qBittorrent. Delete downloaded content only after
   confirming MediaManager has imported or the fixture is being discarded.
1. Remove imported test files from the test media library if they should not
   remain available, then run an OnScreen library scan.
1. Remove MediaManager test items or release history only through safe UI/API
   paths and only in the test library.
1. Delete Screenarr SQLite rows only in a disposable test database, with
   Screenarr stopped, after confirming `SCREENARR_DATA_PATH` is not shared with
   real requests.
1. Delete or redact local evidence artifacts that contain secrets, cookies,
   signed headers, credentials, database dumps, or raw config.

## Useful Checks

```bash
curl -X POST \
  -H "X-Api-Key: $BRIDGE_API_KEY" \
  http://localhost:7879/api/bridge/v1/queue/QUEUE_ID/reconcile
```

```bash
curl -H "X-Api-Key: $BRIDGE_API_KEY" \
  http://localhost:7879/api/bridge/v1/queue/QUEUE_ID/events
```

Open the dashboard at:

```text
http://localhost:7879/dashboard/login
```

Use the dashboard to refresh candidates, submit a candidate, reconcile a queue
item, and view recent events.
