# PROJECT KNOWLEDGE BASE

Generated: 2026-07-04T06:31:51Z
Commit: 475f838
Branch: main

## OVERVIEW

Screenarr is a small Python/FastAPI compatibility bridge between stock OnScreen
and MediaManager. Keep it a bridge: do not fork, patch, or vendor OnScreen or
MediaManager behavior into this repo.

## STRUCTURE

```text
./
|-- bridge/                 # FastAPI app, Arr-compatible API, SQLite state
|-- tests/                  # Request-flow, queue, webhook, review-gate tests
|-- docs/                   # Install, security, E2E, roadmap, troubleshooting
|-- scripts/review-gate.ps1 # Local publish gate including CodeRabbit parsing
|-- Dockerfile              # Runs uvicorn bridge.main:create_app --factory
|-- docker-compose.example.yml
|-- config.example.yaml     # Public config shape and profile terminology
`-- AGENTS.md
```

## WHERE TO LOOK

| Task | Location | Notes |
| --- | --- | --- |
| App factory, routes, queue actions | `bridge/main.py` | Largest file; route wiring plus orchestration helpers. |
| Settings and profile schema | `bridge/config.py` | Env validation, profile modes, secret trimming. |
| SQLite queue/state | `bridge/store.py` | Status transitions, candidate batches, event redaction. |
| MediaManager calls | `bridge/mediamanager.py` | HTTP client, login refresh, release picking. |
| Dashboard markup | `bridge/dashboard.py` | HTML rendering only; auth lives in `main.py`/`security.py`. |
| HMAC/session checks | `bridge/security.py` | Dashboard tokens and OnScreen webhook signatures. |
| Config drift checks | `bridge/validation.py` | MediaManager config parsing and warning codes. |
| Arr payload models | `bridge/arr_models.py` | Radarr/Sonarr-compatible request/response shapes. |
| Main behavior tests | `tests/test_queue_and_validation.py` | Queue, dashboard, webhook, reconciliation, validation. |
| Arr API tests | `tests/test_arr_api.py` | Compatibility endpoints and MediaManager client flow. |
| Review gate tests | `tests/test_review_gate_script.py` | CodeRabbit NDJSON fixture coverage. |

## CODE MAP

Static map only; Python LSP was not installed during init-deep.

| Symbol | Type | Location | Role |
| --- | --- | --- | --- |
| `create_app` | function | `bridge/main.py` | FastAPI factory and dependency wiring. |
| `download_queue_candidate_action` | function | `bridge/main.py` | Dashboard/API release submit state machine. |
| `reconcile_queue_item` | function | `bridge/main.py` | MediaManager import polling transition. |
| `apply_onscreen_availability_hint` | function | `bridge/main.py` | Signed webhook availability transition. |
| `BridgeStore` | class | `bridge/store.py` | SQLite migrations, queue rows, events, candidates. |
| `MediaManagerClient` | class | `bridge/mediamanager.py` | Authenticated MediaManager API wrapper. |
| `Settings` | class | `bridge/config.py` | Environment model and secret validation. |
| `BridgeConfig` | class | `bridge/config.py` | YAML config and profile validation. |
| `verify_onscreen_signature` | function | `bridge/security.py` | HMAC and replay-window verification. |
| `validate_static_config` | function | `bridge/validation.py` | Config and optional MediaManager drift checks. |

## CONVENTIONS

- Preserve the Arr-compatible API surface OnScreen already calls.
- Use TRaSH/Profilarr-style labels for user-facing profile names and metadata,
  but keep MediaManager as the source of truth for scored release candidates.
- Prefer standard-library storage and parsing unless a new dependency removes
  meaningful complexity.
- Keep Docker deployment single-container unless a feature truly requires a
  separate service.
- Add tests for every request-flow, auth, queue, webhook, validation, or
  MediaManager side-effect change.
- Treat `download_unverified` as ambiguous submit state. Reconcile before
  retrying so the bridge does not create duplicate grabs.
- OnScreen webhooks are hints only. They can confirm availability after import;
  they must not fail, delete, or reopen requests by themselves.

## ANTI-PATTERNS

- Direct qBittorrent, Prowlarr, Internet Archive, or download-client behavior in
  Screenarr. Route acquisition through MediaManager.
- Auth shortcuts on Arr-compatible endpoints, dashboard endpoints, queue APIs, or
  webhook handling.
- Secret leaks in logs, docs, fixtures, review output, cookies, or signed payload
  material.
- Queue updates without expected-status guards when stale workers or duplicate
  requests could race.
- Candidate refreshes that delete old candidates before validating the complete
  replacement batch.
- Treating Jackett as the happy path; docs and validation prefer Prowlarr.

## COMMANDS

```bash
python -m ruff check .
python -m pytest
docker build -t screenarr:local .
powershell -ExecutionPolicy Bypass -File scripts/review-gate.ps1 -CodexReviewConfirmed
```

The review gate is a thin adapter to the central CodeRabbit runner configured
via the gate's `-CentralCodeRabbitRunner` parameter (default: a central checkout
outside this repo). That central runner is the only CodeRabbit invocation owner;
it selects the explicit uncommitted scope, enforces quota and replay policy, and
uses the authenticated Debian CLI without repository credentials or Docker
fallbacks.

## REVIEW GUIDELINES

Treat these as blocking findings for Codex and CodeRabbit:

- Auth bypasses in Arr-compatible endpoints, dashboard endpoints, queue APIs, or
  OnScreen webhook handling.
- Secret leaks, including bridge API keys, MediaManager credentials, webhook
  secrets, CodeRabbit credentials, cookies, or signed payload material.
- Unsafe webhook behavior: unsigned payloads, missing replay checks, or
  destructive queue changes from webhook data alone.
- MediaManager side-effect bugs: downloading before bridge approval, calling the
  wrong movie/show endpoint, or changing libraries unexpectedly.
- Queue idempotency bugs: duplicate queue rows, stale-write overwrites, or lost
  release candidates.
- Missing tests for new queue states, dashboard auth, webhook verification,
  MediaManager request translation, validation checks, or review-gate behavior.

Before publishing to GitHub, the working tree must pass Ruff, pytest, Docker
build, local secret scan, Codex review, and CodeRabbit review.
