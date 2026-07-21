# Security Notes

Screenarr is meant to run on a private Docker network next to OnScreen and
MediaManager.

## Secrets

Do not commit:

- `.env`
- `config.yaml`
- real MediaManager tokens
- real MediaManager passwords
- real bridge API keys
- real OnScreen webhook secrets
- dashboard cookies
- CodeRabbit credentials

The repo ignores `.env`, `config.yaml`, SQLite state files, and local review
outputs by default.

## Dashboard

The dashboard is disabled unless `ENABLE_DASHBOARD=true`. Dashboard sessions are
signed with `DASHBOARD_SESSION_SECRET` and stored in an HTTP-only cookie. If
that secret is blank, Screenarr generates a signing secret and persists it in
the SQLite state file. Rotate `DASHBOARD_SESSION_SECRET`, or clear the generated
`dashboard_session_secret` value from `schema_meta`, to invalidate existing
dashboard sessions.

`BRIDGE_API_KEY` is separate. It authenticates OnScreen's Arr-service requests
and dashboard login attempts, but it does not sign dashboard cookies.

Dashboard and login responses carry defense-in-depth headers: a
`Content-Security-Policy` that allows only the inline styles the server-rendered
pages need (no scripts), `X-Frame-Options: DENY`, `X-Content-Type-Options:
nosniff`, `Referrer-Policy: no-referrer`, and `Cache-Control: no-store`.

Dashboard logins are throttled per client IP: after 5 consecutive failed
attempts, further logins from that IP are rejected with HTTP 429 for 60
seconds. A successful login resets the counter. The throttle is in-memory and
requires single-worker deployment — the reference Dockerfile/Compose run a
single uvicorn worker by design (SQLite is single-writer too), and a restart
clears the counters. Multi-replica deployments need shared atomic storage for
throttle state instead. It applies only to `/dashboard/login`; the
Arr-compatible API-key endpoints used by OnScreen are not throttled.

## Reverse Proxy And Forwarded Headers

By default (`TRUST_FORWARDED_HEADERS=false`) Screenarr ignores client-supplied
`X-Forwarded-Host` and `X-Forwarded-Proto`. Dashboard CSRF origin checks and
the session-cookie `Secure` flag are derived from the direct request URL, so a
directly exposed bridge cannot be tricked by spoofed forwarded headers.

Set `TRUST_FORWARDED_HEADERS=true` only when the bridge sits behind a trusted
reverse proxy that terminates TLS and sets — or strips and re-sets —
`X-Forwarded-Host` and `X-Forwarded-Proto` on every request. With the setting
on, Screenarr trusts those headers for the CSRF origin check and for marking
the dashboard cookie `Secure`. Never enable it on a directly exposed bridge:
any client could then spoof the headers and weaken both protections.

## OnScreen Webhook

The optional OnScreen webhook receiver is disabled unless
`ENABLE_ONSCREEN_WEBHOOK=true`. When enabled, requests must include:

- `X-OnScreen-Timestamp`
- `X-OnScreen-Signature`

The signature is `sha256=<HMAC(ONSCREEN_WEBHOOK_SECRET, "{timestamp}.{body}")>`.
Screenarr rejects timestamps outside a 5-minute replay window.

## Ports

For local use, bind the bridge to localhost:

```yaml
ports:
  - "127.0.0.1:7879:7879"
```

If you expose the write-capable dashboard to a LAN, keep it on a private
network and put it behind a trusted reverse proxy with authentication and IP
allow-listing. Browser-driven mutating dashboard actions also depend on CSRF
protection: the mutating handlers in `bridge/main.py` must keep requiring
`require_dashboard_csrf`. `bridge/dashboard.py` is a rendering helper only; it
does not enforce auth or CSRF. The dashboard can approve requests, submit
downloads, refresh candidates, and reconcile queue items.
