# TEST KNOWLEDGE

## OVERVIEW

`tests/` is the contract for Screenarr behavior. Add or update tests with every
auth, queue, webhook, reconciliation, validation, or MediaManager API change.

## WHERE TO LOOK

| Task | Location | Notes |
| --- | --- | --- |
| Arr-compatible endpoints | `test_arr_api.py` | Auth, lookup, service metadata, MediaManager request paths. |
| Queue and dashboard flows | `test_queue_and_validation.py` | Largest behavioral suite; use existing helpers. |
| Webhook/security flows | `test_queue_and_validation.py` | Signature, replay, dashboard session, availability hints. |
| Review gate behavior | `test_review_gate_script.py` | Fixture-driven PowerShell/CodeRabbit parsing tests. |
| CodeRabbit fixtures | `fixtures/*.ndjson` | One fixture per parser outcome; keep them small. |

## CONVENTIONS

- Prefer `make_flow_client`, `settings_for`, `post_media_and_first_queue_item`,
  and existing fake MediaManager classes over new ad hoc setup.
- Put queue state-machine tests near related queue tests; avoid scattering
  duplicated flows.
- For webhook tests, sign payloads with `sign_webhook` and keep test values
  obviously non-secret.
- For review-gate tests, add an NDJSON fixture first, then assert the script
  exit behavior.
- Assert both stored queue state and API response when testing transitions.
- Preserve idempotency coverage for duplicate movie and show requests.

## ANTI-PATTERNS

- Weakening fixtures or assertions to satisfy the gate.
- Using real credentials or plausible production tokens in fixtures.
- Mocking away the store when a bug is about persisted queue/candidate/event
  state.
- Treating malformed CodeRabbit NDJSON as ignorable if any valid line exists.
- Assuming dashboard query-string API keys are allowed; dashboard auth rejects
  query API keys and uses header or session cookie auth.
