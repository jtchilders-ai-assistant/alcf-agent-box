---
name: alcf-iri-facility-api
description: Interact with the ALCF IRI Facility API (api.alcf.anl.gov) — compute jobs, filesystem operations, account data, and facility status. Uses the official alcf-tokens Globus credential in Agent in a Box.
category: research
---

# ALCF IRI Facility API

The ALCF Facility API (a.k.a. IRI API) at `https://api.alcf.anl.gov` is ALCF's implementation
of the DOE Integrated Research Infrastructure (IRI) Facility API standard. It gives
programmatic access to compute (Polaris, Crux), filesystems (Home, Eagle), account/project/
allocation metadata, and facility/resource status.

- **Base URL:** `https://api.alcf.anl.gov/api/v1`
- **OpenAPI spec:** `https://api.alcf.anl.gov/openapi.json` (title "ALCF implementation of the IRI Facility API")
- **User docs:** https://docs.alcf.anl.gov/services/iri-api/
- **Auth:** Globus (OAuth2, command-line login flow). Access tokens valid 48h, auto-refreshing.
- **Auth package:** https://pypi.org/project/alcf-tokens/ (`alcf-tokens==0.3.0` in this image)

## Stable resource IDs (verified live 2026-07)

    Polaris  55c1c993-1124-47f9-b823-514ba3849a9a   (compute)
    Crux     8b9b42f7-572a-4909-8472-a0453436304c   (compute)
    Aurora   0325fc07-6fb7-4453-b772-3d5030b2df72   (compute)
    Sophia   9674c7e1-aecc-4dbb-bf01-c9197e027cd6   (compute)
    Eagle    1c3ad9d4-2e91-42bc-becb-72b1fde1235c   (storage)
    Home     6115bd2c-957a-4543-abff-5fae52992ff2   (storage)

Never hardcode blindly — re-verify with `GET /status/resources` (no auth required).

## Endpoint groups (public vs authenticated)

- **No auth:** `GET /status/resources`, `/status/resources/{id}`, `/facility`, `/facility/sites`,
  `/status/events`, `/status/incidents`. Good smoke tests.
- **Auth required (Bearer token):** everything under `/compute`, `/filesystem`, `/account`, `/task`.

## Authentication (Globus command-line flow)

The official package uses `globus_sdk.UserApp` with the **command-line login flow**: it prints a URL,
you open it in a browser + log in with ALCF creds, then paste the resulting authorization code
back into the waiting process. It does NOT spin up a local browser server, so it works headless
— but it is INTERACTIVE and cannot complete non-interactively (no way to script the code entry).

Full driving procedure (setup + PTY-based interactive auth) is in
`references/iri-api-validation.md`. Quick version:

    alcf-tokens login                    # one login for all ALCF services
    access_token=$(alcf-tokens get-token iri)  # 48h, auto-refresh

Tokens persist under the official package's Globus app store on the mounted home volume.

### Driving the interactive auth from a Hermes session (the working pattern)

You cannot pipe the code in ahead of time — each run generates a FRESH PKCE `code_challenge`,
so a URL from a previously-killed process is useless (its code fails with
`invalid_grant: code_verifier does not match`). Instead:

1. Start auth as a **background PTY process**: `terminal(background=true, pty=true, command="alcf-tokens login")`.
2. `process(action=wait, timeout=8)` to capture the URL it prints.
3. Give the user THAT URL. Wait for them to paste the code back.
4. `process(action=submit, data="<code>", session_id=...)` — submit sends the code + Enter.
5. `process(action=wait)` — exit code 0 = success. Verify with `alcf-tokens test-token iri`.

## Compute job lifecycle (Polaris/Crux)

- **Submit:** `POST /compute/job/{resource_id}` with a JSON body (executable, arguments,
  name, stdout_path, stderr_path, resources.node_count, attributes.{duration, queue_name,
  account, custom_attributes.filesystems}). Returns **HTTP 200** with the PBS job id
  (`NNNNNNN.polaris-pbs-01...`) and initial state. `account` = the PROJECT NAME (your
  ALCF project/allocation — look it up via `GET /account/projects`); `queue_name` = a real
  PBS queue (e.g. `debug`); stdout/stderr paths must
  exist first (mkdir them). Job body maps to a `qsub` script minus `#PBS` directives.
- **Get one job (reliable):** `GET /compute/status/{resource_id}/{job_id}?historical=true`. Use
  this to track a specific job. States seen: queued → active → completed. A cancelled job
  reports terminal state `completed`.
- **List jobs (unreliable for finding a specific job):** `POST /compute/status/{resource_id}`
  with `?historical=&limit=&offset=`. Capped at 200 rows; ordering is such that a
  just-submitted job may NOT appear even with historical=true. Do not rely on it to find your
  own job — use get-by-id instead.
- **Cancel:** `DELETE /compute/cancel/{resource_id}/{job_id}` → **HTTP 204** (accepted, async).
  The job stays `active` for a few seconds before PBS reports it terminal; 204 means "accepted",
  not "already cancelled".

## Filesystem ops (Home/Eagle) — ALL ASYNCHRONOUS

Every `/filesystem/*` call returns a `task_id` immediately; you then POLL
`GET /task/{task_id}` until `status` is `completed`/`failed`. The RESULT (and any error) lives
in the task, not the submit response — a submit can return 200 while the task later FAILS.

- Working: `ls` (GET), `mkdir` (POST, returns 201), `view` (GET, bytes), `head` (GET, lines),
  `rm` (DELETE), `chown` (PUT), `chmod` (PUT).
- **Per-identity path allowlist:** requests outside your permitted roots return a task_id, then
  the task FAILS with `Path must start with one of: /home/<you>, /eagle, /lus/eagle`. The
  allowed home is YOUR ALCF username's home, not an arbitrary one. This is expected, not a bug.

## Pitfalls

- **ONE token covers the whole API — there is NO separate "compute" scope.**
  The auth script requests a scope whose name ends in `/filesystem`
  (`https://auth.globus.org/scopes/6be511f6-…/filesystem`), but that SAME token
  authorizes `/compute/*`, `/account/*`, and `/filesystem/*`. The OpenAPI spec
  uses a single `HTTPBearer` security scheme with no per-endpoint scopes, and the
  ALCF docs use this exact token for `POST /compute/job`. DO NOT invent a
  compute scope or a second client id — an LLM seeing "filesystem" in the scope
  string has been observed hallucinating a fake compute client id and telling
  the user to edit the script. That is wrong.
- **403 "error code: 1010" = CLOUDFLARE bot-block, NOT auth.** api.alcf.anl.gov
  is behind Cloudflare, which 403s requests with a default `Python-urllib/3.x`
  User-Agent (body: `{"error":"error code: 1010\n"}`). The SAME token returns 200
  via curl. Fix: send a normal `User-Agent` header on every urllib/requests call
  (the bundled `iri_api_client.py` does). Do NOT diagnose a 1010 as a scope,
  consent, identity, or allocation problem, and do NOT send the user to re-auth
  or ALCF support for it. (A real token failure is 401 "Globus token not active"
  or a 403 whose body is NOT "error code: 1010".) Verified 2026-07-30.
- **`wget` is not on stock macOS** — the docs' `wget <url>` for the auth script fails on Macs.
  Use `curl -O <url>` (or `curl -sL <url> -o file.py`).
- **A 200 on a filesystem submit is not success** — you MUST poll the task; it can still fail
  (bad path, allowlist, etc.). Only the task's terminal status tells you what happened.
- **Cancel/filesystem are async** — don't assert success from the immediate HTTP code alone.
- **Job list won't show your fresh job** — use `GET /compute/status/{res}/{job}` to track it.
- **OpenAPI spec is ahead of implementation.** ~42 routes in the spec, ~18 documented. Some
  extra routes are live (`account/capabilities`, `status/events`, `status/incidents`,
  `facility/sites`, `GET /task` list, nested `user_allocations`) but several are STUBS
  returning **HTTP 501 "Command <x> option not implemented yet"**: `filesystem/stat`,
  `filesystem/tail`, `filesystem/checksum` (and likely `cp`/`mv`/`symlink`/`compress`/
  `extract`/`upload`/`download` — untested). Don't assume a spec route works; probe it.
- **Not Argo, not AmSC.** Argo = LLM inference (username-as-key). AmSC MAG = LiteLLM proxy
  (Ping/Dex). IRI Facility API = Globus auth. Three distinct ANL systems.
- **Distinct credential, shared login.** An Inference token sent to IRI returns
  HTTP 401 and vice versa; select the credential for the target service. The
  image's default `alcf-tokens login` obtains both in one interactive flow.

## Files

- `references/iri-api-validation.md` — full validation transcript + findings (what works,
  doc discrepancies, tested return codes) and the step-by-step interactive-auth recipe.
- `scripts/iri_api_client.py` — reusable helper: token load, async task poll, thin wrappers
  for status/account/compute/filesystem calls.
