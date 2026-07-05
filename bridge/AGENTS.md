# BRIDGE PACKAGE KNOWLEDGE

## OVERVIEW

`bridge/` contains the runtime app. Most behavior flows through `main.py`, with
state, API client, config, security, validation, dashboard rendering, and Arr
models split into focused modules.

## WHERE TO LOOK

| Task | Location | Notes |
| --- | --- | --- |
| FastAPI app factory and routes | `main.py` | `create_app` owns lifespan, auth dependencies, and routes. |
| Queue actions | `main.py` | Approval, manual download, reconcile, webhook transitions. |
| SQLite schema/state | `store.py` | Migrations, guarded updates, candidates, audit events. |
| MediaManager HTTP | `mediamanager.py` | Login refresh, endpoints, timeout handling, release choice. |
| Environment/config | `config.py` | Pydantic settings and profile validation. |
| Dashboard HTML | `dashboard.py` | Rendering helpers only; keep side effects out. |
| HMAC/session auth | `security.py` | Constant-time checks and digest validation. |
| Static/live validation | `validation.py` | Config warnings and MediaManager library checks. |
| Compatibility schemas | `arr_models.py` | Keep payload names compatible with Radarr/Sonarr callers. |

## CONVENTIONS

- Keep orchestration async at route/action boundaries; use `store_call` for
  blocking SQLite work.
- Guard state transitions with expected statuses when a worker, dashboard click,
  or duplicate request could race.
- Store audit details through `BridgeStore.add_event` so message/payload
  redaction is applied.
- Use `MediaManagerError` for application-level MediaManager failures and
  `httpx.TransportError` handling for transport failures.
- Dashboard route handlers should check `ENABLE_DASHBOARD` and use
  `require_dashboard_auth`; mutating dashboard handlers must also require
  `require_dashboard_csrf`.
- Signed OnScreen webhook code must verify timestamp and signature before JSON
  parsing side effects.
- Do not mark a request `available` from webhook data unless it was already
  reconciled to `imported`.

## ANTI-PATTERNS

- Calling qBittorrent, Prowlarr, or indexer URLs directly from this package.
- Returning raw upstream secrets, signed URLs, tokens, cookies, or credentials in
  API errors or events.
- Deleting candidate rows before validating the full replacement batch.
- Letting a bulk reconcile abort on one item; record the failure and continue.
- Moving dashboard auth logic into `dashboard.py`; that file should render only.
