# IRI API validation notes (session 2026-07-17, identity `parton`)

End-to-end validation of every documented command at https://docs.alcf.anl.gov/services/iri-api/
against the live API. All 18 documented endpoints worked. Issues below are doc gaps / rough
edges, not broken functionality.

## Setup that worked

    mkdir -p /tmp/iri_test && cd /tmp/iri_test
    python3 -m venv venv && source venv/bin/activate
    pip install globus-sdk requests          # globus_sdk 4.8.1, py3.12
    # docs say `wget ...` — FAILS on macOS (no wget). Use curl:
    curl -sL https://raw.githubusercontent.com/argonne-lcf/alcf-facility-api-token/refs/heads/main/alcf_facility_api_globus_token.py -o alcf_facility_api_globus_token.py

## Interactive auth recipe (the working pattern in a Hermes session)

Each `authenticate` run generates a fresh PKCE `code_challenge`. A URL from a killed process
is dead — its code fails `invalid_grant: code_verifier does not match`. So keep ONE process
alive across the browser round-trip:

1. `terminal(background=true, pty=true, command="cd /tmp/iri_test && source venv/bin/activate && python alcf_facility_api_globus_token.py authenticate")`
2. `process(action=wait, session_id=..., timeout=8)` → captures the printed Globus URL.
3. Hand THAT URL to the user; they log in with ALCF creds and paste the auth code back.
4. `process(action=submit, session_id=..., data="<code>")` (submit = data + Enter).
5. `process(action=wait, session_id=...)` → exit 0 = success.
6. Verify: `access_token=$(python alcf_facility_api_globus_token.py get_access_token)` (len ~91,
   prefix `Agx...`); `... get_time_until_token_expiration --units hours` → `48.0`.

Token cache: `~/.globus/app/8b84fc2d-49e9-49ea-b54d-b3a29a70cf31/alcf_facility_api_app/tokens.json`

## Validation results (all documented commands PASS)

| # | Command | Method | Result |
|---|---------|--------|--------|
| 1.1 | status/resources | GET | 200 — 10 resources |
| 1.2 | status/resources/{id} | GET | 200 |
| 1.3 | facility | GET | 200 |
| 2.1 | compute/job/{res} submit | POST | 200 — real PBS job created |
| 2.2 | compute/status/{res} list | POST | 200 (see pagination note) |
| 2.3 | compute/status/{res}/{job} | GET | 200 — tracked queued→active→completed |
| 2.4 | compute/cancel/{res}/{job} | DELETE | 204 |
| 3.1 | filesystem/ls | GET | task completed |
| 3.2 | filesystem/mkdir | POST | 201, task completed |
| 3.3 | filesystem/view | GET | task completed (bytes) |
| 3.4 | filesystem/head | GET | task completed (lines) |
| 3.5 | filesystem/chown | PUT | not tested (avoided perms changes) |
| 3.6 | filesystem/chmod | PUT | not tested (avoided perms changes) |
| 3.7 | filesystem/rm | DELETE | task completed |
| 4.1 | task/{id} | GET | 200 (used throughout) |
| 5.1 | account/projects | GET | 200 |
| 5.2 | account/projects/{id} | GET | 200 |
| 5.3 | .../project_allocations | GET | 200 |
| 5.4 | .../project_allocations/{id} | GET | 200 |

Compute proof: submitted 10s sleep to Polaris (queue `debug`, a real project account) →
job `7260513.polaris-pbs-01...` → queued→active → DELETE cancel (204) → terminal `completed`
→ removed `.OU`/`.ER` output files. Job left a 10-byte stdout (`Start\n`) before cancel landed.

Account note: look up your project + remaining allocation via `GET /account/projects` and
`.../project_allocations`. An empty allocation list (`[]`) is a valid 200 response.

## Doc discrepancies found (report these upstream)

1. `wget` fails on macOS — offer `curl -O <url>` alongside.
2. Token-lifetime contradiction: docs page says refreshed tokens last "up to 7 days"; the auth
   script's own docstring says the refresh token expires after "6 months of inactivity". ~26×
   disagreement — reconcile.
3. `compute/status` list is capped (200 rows even with limit=200) and ordered so a fresh job
   isn't in it, even with historical=true. Document max page size + ordering, and steer users
   to get-by-id for tracking a specific job.
4. Cancel is async: 204 = accepted, job stays `active` briefly. Add a note.
5. Return codes not stated: mkdir=201, submit=200, cancel=204.
6. Filesystem path allowlist is undocumented: off-allowlist paths return a task_id then the
   task FAILS with `Path must start with one of: /home/<you>, /eagle, /lus/eagle`.
7. OpenAPI spec (42 routes) is ahead of implementation (18 documented). Live extras:
   account/capabilities, status/events, status/incidents, facility/sites, GET /task list,
   nested user_allocations. STUBS returning 501 "not implemented yet": filesystem/stat,
   filesystem/tail, filesystem/checksum (likely also cp/mv/symlink/compress/extract/upload/
   download — untested).
