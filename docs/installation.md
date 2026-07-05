# Install And Deploy

Screenarr is only the bridge. It does not replace OnScreen, MediaManager,
Prowlarr, a download client, or your media folders.

## 1. Prepare The Config

Copy the sample files:

```bash
cp .env.example .env
cp config.example.yaml config.yaml
```

Set these values in `.env`:

```dotenv
BRIDGE_API_KEY=make-a-long-random-value
MEDIAMANAGER_BASE_URL=http://mediamanager:8000
MEDIAMANAGER_TIMEOUT_SECONDS=120
MEDIAMANAGER_USERNAME=
MEDIAMANAGER_PASSWORD=
MEDIAMANAGER_TOKEN=
ENABLE_DASHBOARD=false
DASHBOARD_SESSION_SECRET=
SCREENARR_DATA_PATH=/data/screenarr.db
ENABLE_ONSCREEN_WEBHOOK=false
ONSCREEN_WEBHOOK_SECRET=
ENABLE_MEDIAMANAGER_RECONCILE=false
MEDIAMANAGER_RECONCILE_INTERVAL_SECONDS=300
```

Use either `MEDIAMANAGER_TOKEN` or `MEDIAMANAGER_USERNAME` plus
`MEDIAMANAGER_PASSWORD`.

`MEDIAMANAGER_TIMEOUT_SECONDS` controls MediaManager detail lookups used by
reconciliation. Inline add, list, search, and download-submission calls are
capped at 30 seconds by `FOREGROUND_TIMEOUT_SECONDS` in
`bridge/mediamanager.py`.

When `ENABLE_ONSCREEN_WEBHOOK=true`, set `ONSCREEN_WEBHOOK_SECRET` to a
high-entropy shared secret at least 32 characters long. Do not leave it blank
in webhook mode.

If profiles target non-default MediaManager libraries, use a MediaManager
superuser credential. MediaManager protects library changes behind superuser
permissions.

## 2. Start The Container

```bash
docker compose up -d --build
```

The default Compose example assumes MediaManager runs on the host and uses
`http://host.docker.internal:8000`. Docker Desktop provides that hostname by
default; plain Linux Engine needs the included `extra_hosts:
host.docker.internal:host-gateway` mapping. If MediaManager is in the same
Compose project, prefer its service name instead, such as
`http://mediamanager:8000`.

The bridge listens on:

```text
http://localhost:7879
```

The optional dashboard is:

```text
http://localhost:7879/dashboard/login
```

## 3. Add It To OnScreen

Create two Arr-service entries in OnScreen. Both point to the same Screenarr URL
and API key:

- `Screenarr Radarr`: kind `Radarr`, for movies
- `Screenarr Sonarr`: kind `Sonarr`, for shows
- URL for both: `http://screenarr:7879` when OnScreen and Screenarr share a Docker network, otherwise `http://host.docker.internal:7879` from an OnScreen container to a host-run Screenarr, or `http://localhost:7879` when OnScreen runs directly on the host
- API key for both: same value as `BRIDGE_API_KEY`

Pick the Screenarr quality profile that matches the behavior you want.

## 4. How Requests Flow

```text
OnScreen request
-> OnScreen Arr service
-> Screenarr
-> MediaManager
   -> Prowlarr
   -> download client
   -> file appears in your media folder
-> OnScreen library scan marks it available
```

Screenarr talks to OnScreen and MediaManager only; MediaManager owns the
Prowlarr and download-client path.

Jackett may still work through MediaManager compatibility, but use Prowlarr as
the primary indexer path for new deployments.

## 5. Optional Dashboard

Set `ENABLE_DASHBOARD=true` to enable `/dashboard`. Leave it `false` to keep
the dashboard disabled by default.

Set `DASHBOARD_SESSION_SECRET` to a high-entropy value at least 32 characters
long if dashboard sessions should survive container restarts. If it is blank,
Screenarr generates one and persists it in the SQLite state file.

Local browser testing can use the login page:

```text
http://localhost:7879/dashboard/login
```

API clients can create and reuse a session cookie with:

```bash
curl -c cookies.txt -b cookies.txt \
  -X POST http://localhost:7879/dashboard/login \
  -H "Content-Type: application/json" \
  -d '{"api_key":"YOUR_BRIDGE_API_KEY"}'

curl -b cookies.txt http://localhost:7879/dashboard
```

The dashboard shows status, profiles, root folders, tags, validation warnings,
release candidates, selected release events, latest errors, and the
manual/approval queue. It can approve requests, submit selected release
candidates to MediaManager, refresh candidates, and reconcile submitted items.

Set `ENABLE_MEDIAMANAGER_RECONCILE=true` only if you want Screenarr to poll
MediaManager in the background. Manual reconcile buttons and API endpoints are
available without enabling background polling.
