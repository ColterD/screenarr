# Development Process

Screenarr changes should be reviewed locally before they are committed and
published to GitHub.

## Required pre-publish gate

Run:

```powershell
.\scripts\review-gate.ps1 -CodexReviewConfirmed
```

The gate runs:

- repository sanity checks
- secret scan
- Ruff
- pytest
- Docker image build
- CodeRabbit review delegated to the central runner

Codex local review is interactive in the Codex app, so run `/review` against
the uncommitted changes first. Pass `-CodexReviewConfirmed` only after Codex
P0/P1 issues are resolved or explicitly documented as accepted.

## CodeRabbit

`scripts/review-gate.ps1` delegates exactly once to the shared central runner
with an explicit uncommitted scope and this repository's complete
`.coderabbit.yaml` fallback configuration. The runner path resolves from
`-CentralCodeRabbitRunner`, then the `SCREENARR_CENTRAL_CODERABBIT_RUNNER`
environment variable, then an example default checkout at
`D:\Projects\coderabbit\Invoke-CodeRabbit.ps1`; contributors on other machines
must pass `-CentralCodeRabbitRunner` or set the environment variable. The
central runner owns Debian CLI
authentication, native review worktrees, diff hashing, findings replay, quota
reservation, redaction, and fail-closed NDJSON parsing. Screenarr does not carry
a second CLI, Docker image, API key, or runner-selection path.

Use `-SkipCodeRabbit` only when an existing PR lane or another task owner has
already reviewed the identical diff hash. Resolve all `critical` and `major`
findings before committing. If a finding is intentionally accepted, document the
reason in the commit or PR notes; minor and trivial findings do not justify an
extra paid review.

## GitHub review

After pushing a PR, request Codex review with:

```text
@codex review
```

Automatic Codex PR review can also be enabled in the GitHub integration. PRs
should not merge until Codex P0/P1 and CodeRabbit critical/major findings are
resolved or explicitly accepted.
