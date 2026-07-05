# DOCS KNOWLEDGE

## OVERVIEW

`docs/` is operator and contributor documentation. Keep docs aligned with the
actual bridge boundary: Screenarr talks to OnScreen and MediaManager, not to
download clients or indexers directly.

## WHERE TO LOOK

| Task | Location | Notes |
| --- | --- | --- |
| Install/deploy | `installation.md` | Docker, env vars, OnScreen Arr-service setup. |
| Local E2E | `e2e-local-test.md` | Public-domain test flow and expected queue states. |
| Security | `security.md` | Secrets, dashboard auth, webhook signing, exposure guidance. |
| Troubleshooting | `troubleshooting.md` | Operational symptoms and safe next checks. |
| Roadmap | `roadmap.md` | Staged future work; keep preview posture clear. |
| Contribution process | `development-process.md` | Ruff, pytest, Docker, Codex review, CodeRabbit review. |

## CONVENTIONS

- Lead with development-preview language; this is not a supported public release.
- Prefer Prowlarr as the documented happy path. Mention Jackett only as a
  compatibility fallback through MediaManager.
- Keep TRaSH/Profilarr content metadata-first unless a stable public API is
  explicitly available and implemented.
- Explain queue states consistently: `download_submitted`,
  `download_unverified`, `download_failed`, `imported`, `available`.
- Troubleshooting should identify which component owns the fix: OnScreen,
  Screenarr, MediaManager, Prowlarr, or qBittorrent.

## ANTI-PATTERNS

- Promising direct Internet Archive, qBittorrent, Prowlarr, or cleaner behavior
  inside Screenarr.
- Publishing secret-like example values. Use placeholders that pass the local
  secret scan.
- Documenting a workaround as a Screenarr feature.
- Moving Jackett back into the primary install path.
- Describing webhook data as authoritative failure data; it is only a signed
  availability hint.
