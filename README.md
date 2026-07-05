# Screenarr

> Development preview: Screenarr is an experimental work-in-progress bridge for
> early testing and collaboration. It is not a supported public release yet, and
> breaking changes should be expected.

Arr-compatible bridge that lets [OnScreen](https://github.com/CollinJAycock/OnScreen)
dispatch built-in media requests to
[MediaManager](https://github.com/maxdorninger/MediaManager) without forking or
patching OnScreen.

OnScreen already speaks the Radarr/Sonarr v3 API for request fulfillment. This
bridge exposes the small subset of that API OnScreen needs, then translates the
request into MediaManager API calls.

## Status

Early MVP.

- Works as a standalone container.
- Exposes Radarr/Sonarr-compatible service, profile, root folder, tag, lookup,
  movie add, and series add endpoints.
- Uses TRaSH / Profilarr-style profile names in the OnScreen Arr-service UI.
- Supports `auto` mode for movie grabs and guarded TV season grabs.
- Supports `manual` mode with persisted release candidates for dashboard picking.
- Supports `approval` mode so the bridge can gate requests before MediaManager.
- Optional local dashboard for operators who want queue, candidate, event, and
  reconciliation visibility.

## Docs

- [Install and deploy](docs/installation.md)
- [Development process](docs/development-process.md)
- [Local E2E test guide](docs/e2e-local-test.md)
- [Roadmap](docs/roadmap.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Security notes](docs/security.md)

## Why a bridge instead of an OnScreen plugin?

OnScreen's current MCP plugin role is notification-oriented: OnScreen calls a
plugin's `notify` tool when events fire. Request approval still dispatches to an
Arr service. A compatibility bridge keeps OnScreen stock while using the
integration point it already has.

## Architecture

```text
OnScreen request UI
  -> OnScreen Arr service
  -> OnScreen MediaManager Bridge
  -> MediaManager
      -> Prowlarr
      -> qBittorrent / SABnzbd
      -> completed media files
  -> media files appear in OnScreen's library path
  -> OnScreen scan confirms availability, optionally nudged by signed webhook hints
```

Screenarr only translates OnScreen Arr-compatible requests into MediaManager
actions. MediaManager owns the downstream Prowlarr, qBittorrent, and SABnzbd
interactions.

Jackett may still work through MediaManager compatibility, but Screenarr docs
and validation treat Prowlarr as the primary indexer path.

## Configuration

Copy `config.example.yaml` to `config.yaml`, edit the profiles, then mount it
at `/config/config.yaml` in Docker.

```yaml
profiles:
  - id: 101
    name: "TRaSH: HD Bluray + WEB 1080p"
    media_types: ["movie", "show"]
    mode: "auto"
    mediamanager_library: "Default"
    mediamanager_ruleset: "default"
    score_set: "default"
    max_results: 10
```

Profile fields intentionally use the language of Profilarr/TRaSH Guides where
possible:

- `name`: what OnScreen shows as the quality profile.
- `mode`: `auto`, `manual`, or `approval`.
- `mediamanager_library`: the MediaManager library label to target.
- `mediamanager_ruleset`: the MediaManager ruleset name.
- `score_set`: a TRaSH/Profilarr-style score-set label for humans and future
  sync integrations.

MediaManager currently applies scoring rules inside its own search flow. The
bridge treats MediaManager's scored releases as the source of truth, then picks
the highest-scoring candidate in `auto` mode.

## Environment

| Variable | Purpose |
| --- | --- |
| `BRIDGE_API_KEY` | API key OnScreen sends as `X-Api-Key`. |
| `MEDIAMANAGER_BASE_URL` | MediaManager URL, e.g. `http://mediamanager:8000`. |
| `MEDIAMANAGER_TIMEOUT_SECONDS` | MediaManager detail/reconcile timeout. Defaults to `120`; inline add/list/search/download calls are capped at `30` seconds. |
| `MEDIAMANAGER_TOKEN` | Optional bearer token for MediaManager. |
| `MEDIAMANAGER_USERNAME` | Username/email for MediaManager JWT login. |
| `MEDIAMANAGER_PASSWORD` | Password for MediaManager JWT login. |
| `CONFIG_PATH` | YAML config path. Defaults to `/config/config.yaml`. |
| `ENABLE_DASHBOARD` | Enables `/dashboard`. Defaults to `false`. |
| `SCREENARR_DATA_PATH` | SQLite queue/state path. Defaults to `/data/screenarr.db`. |
| `DASHBOARD_SESSION_SECRET` | Optional high-entropy cookie signing secret. If blank, Screenarr generates one and persists it in SQLite state. |
| `DASHBOARD_SESSION_TTL_MINUTES` | Dashboard login cookie lifetime. Defaults to `720`. |
| `ENABLE_ONSCREEN_WEBHOOK` | Enables signed OnScreen webhook receiver. Defaults to `false`. |
| `ONSCREEN_WEBHOOK_SECRET` | High-entropy shared secret for OnScreen webhook signatures; required to be at least 32 characters when webhooks are enabled. |
| `ENABLE_MEDIAMANAGER_RECONCILE` | Enables background polling for submitted or unverified queue items. Defaults to `false`. |
| `MEDIAMANAGER_RECONCILE_INTERVAL_SECONDS` | Background reconciliation interval. Defaults to `300`. |
| `MEDIAMANAGER_CONFIG_PATH` | Optional read-only MediaManager `config.toml` path for drift checks. |
| `AUTO_DOWNLOAD_FULL_SERIES` | Opt in to full-series auto grabs. Defaults to `false`. |
| `MAX_AUTO_TV_SEASONS` | Safety cap for TV auto grabs. Defaults to `3`. |

Use a MediaManager superuser credential if any Screenarr profile targets a
non-default `mediamanager_library`; MediaManager protects library changes behind
superuser-only endpoints.

## Docker

```yaml
services:
  screenarr:
    image: screenarr:latest
    build: .
    environment:
      BRIDGE_API_KEY: "change-this"
      MEDIAMANAGER_BASE_URL: "http://host.docker.internal:8000"
      MEDIAMANAGER_TIMEOUT_SECONDS: "120"
      MEDIAMANAGER_TOKEN: "YOUR_MEDIAMANAGER_TOKEN"
      # Or leave MEDIAMANAGER_TOKEN blank and use username/password auth:
      MEDIAMANAGER_USERNAME: "YOUR_MEDIAMANAGER_USERNAME"
      MEDIAMANAGER_PASSWORD: "YOUR_MEDIAMANAGER_PASSWORD"
      CONFIG_PATH: "/config/config.yaml"
      ENABLE_DASHBOARD: "false"
      DASHBOARD_SESSION_SECRET: ""
      SCREENARR_DATA_PATH: "/data/screenarr.db"
      ENABLE_MEDIAMANAGER_RECONCILE: "false"
      MEDIAMANAGER_RECONCILE_INTERVAL_SECONDS: "300"
    extra_hosts:
      - "host.docker.internal:host-gateway"
    ports:
      - "127.0.0.1:7879:7879"
    volumes:
      - ./config.yaml:/config/config.yaml:ro
      - screenarr_data:/data

volumes:
  screenarr_data:
```

In OnScreen, configure two Arr-service entries that point to the same bridge.
The API key for both entries is the same value as `BRIDGE_API_KEY`.

When OnScreen and Screenarr share a Docker network:

- `Screenarr Radarr`: kind `Radarr`, for movies, URL `http://screenarr:7879`
- `Screenarr Sonarr`: kind `Sonarr`, for shows, URL `http://screenarr:7879`

When OnScreen runs directly on the host:

- `Screenarr Radarr`: kind `Radarr`, for movies, URL `http://localhost:7879`
- `Screenarr Sonarr`: kind `Sonarr`, for shows, URL `http://localhost:7879`

Use `http://host.docker.internal:7879` only for a host-published Screenarr
container reached from an OnScreen container outside Screenarr's Docker network.

If MediaManager is in the same Compose project, set `MEDIAMANAGER_BASE_URL` to
that service name, for example `http://mediamanager:8000`.

## Dashboard

The dashboard is optional and disabled unless `ENABLE_DASHBOARD=true`.

When enabled, open the login page and enter the same API key as the Arr service:

```text
http://localhost:7879/dashboard/login
```

API clients can create and reuse a dashboard session cookie:

```bash
curl -c cookies.txt -b cookies.txt \
  -X POST http://localhost:7879/dashboard/login \
  -H "Content-Type: application/json" \
  -d '{"api_key":"YOUR_BRIDGE_API_KEY"}'

curl -b cookies.txt http://localhost:7879/dashboard
```

The dashboard shows service status, configured profiles, validation warnings,
queued manual/approval requests, release candidates, selected release events,
latest errors, and manual reconcile controls.

`download_submitted` means MediaManager accepted the grab, not that the file has
been imported. `download_unverified` means the submit outcome is ambiguous, such
as a timeout or transport failure after the request may have reached
MediaManager; reconcile before retrying to avoid duplicate grabs.
`download_failed` means MediaManager reported the grab failed and the request can
be retried manually.
`imported` means MediaManager reports the title downloaded/imported. `available`
means an already imported request was later confirmed available, such as by a
signed OnScreen event; webhook data alone never closes an unreconciled request.

## Roadmap

See [docs/roadmap.md](docs/roadmap.md).
