# Troubleshooting

## Health Check Fails

Run:

```bash
curl http://localhost:7879/healthz
```

Expected:

```json
{"status":"ok"}
```

If this fails, the container is not reachable or did not start.

## OnScreen Says The Arr Service Is Unauthorized

The `BRIDGE_API_KEY` in Screenarr must match the API key saved in OnScreen.
Screenarr accepts the key through the `X-Api-Key` header or `apikey` query
parameter.

## OnScreen Cannot Find Quality Profiles

Check:

```bash
curl -H "X-Api-Key: $BRIDGE_API_KEY" http://localhost:7879/api/v3/qualityprofile
```

If the list is empty or wrong, edit `config.yaml`.

## MediaManager Login Fails

Use one auth method:

```dotenv
MEDIAMANAGER_TOKEN=...
```

or:

```dotenv
MEDIAMANAGER_USERNAME=...
MEDIAMANAGER_PASSWORD=...
```

If both are blank, Screenarr can answer OnScreen setup calls but cannot submit
requests to MediaManager.

## Movies Add But Do Not Download

Check the selected profile mode in `config.yaml`:

```yaml
mode: "auto"
```

`manual` mode adds the title to MediaManager and stores release candidates for
dashboard selection.

`approval` mode stores the request in Screenarr first. Approve it from the
dashboard or bridge queue API before expecting MediaManager activity.

If a selected release stays at `download_unverified`, Screenarr submitted the
grab but could not prove whether MediaManager accepted, rejected, or continued
processing it. Reconcile the queue item before retrying so you do not submit the
same torrent twice:

```bash
curl -X POST \
  -H "X-Api-Key: $BRIDGE_API_KEY" \
  http://localhost:7879/api/bridge/v1/queue/QUEUE_ID/reconcile
```

If MediaManager returns a transient `429` or `5xx`, Screenarr keeps the item in
`download_failed` so the same candidate can be retried after the upstream issue
is fixed. If MediaManager returns a non-transient `4xx`, the request or upstream
configuration usually needs to be fixed before retrying the same submit.

## Shows Add But Full Series Do Not Download

Screenarr has a safety gate for TV. By default it will not automatically grab a
large full-series request.

```dotenv
AUTO_DOWNLOAD_FULL_SERIES=false
MAX_AUTO_TV_SEASONS=3
```

Raise the season cap or set `AUTO_DOWNLOAD_FULL_SERIES=true` only after testing.

## Dashboard Returns 404

Set:

```dotenv
ENABLE_DASHBOARD=true
```

Then recreate the container. When enabled, the dashboard login still requires
the bridge API key; after login, `/dashboard` uses the session cookie. Browser
users can open:

```text
http://localhost:7879/dashboard/login
```

API clients can create a dashboard session:

```bash
curl -c cookies.txt -b cookies.txt \
  -X POST http://localhost:7879/dashboard/login \
  -H "Content-Type: application/json" \
  -d '{"api_key":"YOUR_BRIDGE_API_KEY"}'
```

Then reuse the cookie:

```bash
curl -b cookies.txt http://localhost:7879/dashboard
```

## Queue Is Empty After Container Recreate

Persist the directory that contains your configured `SCREENARR_DATA_PATH`. With
the default `/data/screenarr.db`, persist `/data`:

```yaml
volumes:
  - screenarr_data:/data
```

If you override `SCREENARR_DATA_PATH`, mount and persist that configured
directory instead or the queue database will be recreated with the container.

## qBittorrent Latest Breaks MediaManager Grabs

If MediaManager suddenly cannot add torrents after a qBittorrent container
update, pin qBittorrent to a known-good version instead of `latest`, then retest
MediaManager directly. Screenarr does not talk to qBittorrent; it can only show
whether MediaManager accepted, failed, or timed out during the submit.

## Prowlarr Or Internet Archive Returns 429

Prowlarr is the recommended indexer path. If public indexers such as Internet
Archive return 429 responses, slow down repeated searches and downloads, then
refresh candidates after the rate limit cools off. Screenarr stores the prior
candidate set unless a valid replacement arrives, so a bad refresh should not
erase working choices.

## Internet Archive Torrents Stall On Sidecar Files

Some public-domain Internet Archive torrents include sidecar metadata files
alongside the movie file. If qBittorrent stalls on those small pieces, verify
the playable media file is complete and let MediaManager import from the
completed payload. Treat direct file/torrent handling as an E2E workaround, not
a Screenarr feature.

## OnScreen Cannot Resolve TMDB Metadata

If the request reaches OnScreen but metadata lookup or availability scanning
fails there, check OnScreen's TMDB configuration and throttling. Screenarr only
translates OnScreen's Arr-compatible request to MediaManager; it does not
replace OnScreen's metadata lookup or library scan behavior.

## OnScreen Webhook Returns 401

Check that `ENABLE_ONSCREEN_WEBHOOK=true`, `ONSCREEN_WEBHOOK_SECRET` matches the
OnScreen webhook secret, and the sender includes `X-OnScreen-Timestamp` plus
`X-OnScreen-Signature`. Screenarr also rejects timestamps outside the 5-minute
replay window, so verify the OnScreen host clock is in sync.
