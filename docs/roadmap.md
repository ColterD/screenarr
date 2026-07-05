# Roadmap

Screenarr is a development preview. This roadmap is public so early testers can
see where the bridge is going, but it is not a support promise.

## Phase 1: review gate and docs hardening

- Require local Ruff, pytest, Docker build, secret scan, Codex review, and
  CodeRabbit review before publishing changes.
- Keep the README clear that Screenarr is a development preview.
- Document deployment, troubleshooting, and security expectations.

## Phase 2: persistent queue and dashboard release picking

- Store bridge state in SQLite at `/data/screenarr.db`.
- Keep `auto` mode as direct MediaManager download behavior.
- Let `manual` mode add the title to MediaManager, store scored candidates, and
  wait for an operator to pick a release.
- Expand the optional dashboard from status-only to queue operations.

## Phase 3: bridge-level approval queue

- Let `approval` mode accept OnScreen requests without touching MediaManager.
- Add approve and deny actions independent of OnScreen's own admin approval.
- Keep OnScreen compatibility responses fast and Arr-shaped.

## Phase 4: MediaManager ruleset validation and drift checks

- Validate Screenarr profile libraries against live MediaManager library
  endpoints.
- Optionally parse a read-only MediaManager `config.toml` mount and warn when
  rulesets, rule names, or library mappings drift.

## Phase 5: TRaSH and Profilarr metadata display

- Display TRaSH guide IDs, custom-format group IDs, score-set labels, and
  Profilarr profile IDs as metadata.
- Do not depend on Profilarr sync until stable public APIs are available.

## Phase 6: optional OnScreen event handling

- Accept signed OnScreen webhook/plugin-style events for faster reconciliation.
- Treat events as hints, not authority. Polling and MediaManager state remain
  the reliable source of truth.
