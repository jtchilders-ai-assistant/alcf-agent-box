---
name: alcf-facility-status-and-jobs
description: Report the live state of the user's ALCF work — system up/down + maintenance, their jobs, job output, and allocations.
category: research
---

# ALCF facility status & my-jobs

Read-only "what is the state of MY work on ALCF" answers, using the baked helper
`/opt/alcf/alcf_facility.py` (run with `/opt/hermes/.venv/bin/python`). All four
subcommands are **read-only**. Everything goes through `iri_api_client` (which
sets the User-Agent Cloudflare requires — avoids the 403 "error code: 1010").

Load this skill when the user asks things like: "Is Polaris up?", "Any
maintenance / outages?", "What are my jobs doing?", "Did my job finish / what's
its output?", "How many node-hours do I have left?".

## Commands

    # 1. System status + recent maintenance/outage events — NEEDS NO TOKEN.
    #    Works even before the user has logged in. Lead with this for "is X up".
    /opt/hermes/.venv/bin/python /opt/alcf/alcf_facility.py status

    # 2. List the user's jobs on a cluster (auth). --historical adds finished jobs.
    /opt/hermes/.venv/bin/python /opt/alcf/alcf_facility.py jobs --cluster polaris
    /opt/hermes/.venv/bin/python /opt/alcf/alcf_facility.py jobs --cluster aurora --historical

    # 3. Read a job's stdout/stderr from Home/Eagle (auth). Default reads a byte
    #    view; --lines N does head N. There is NO tail at ALCF, so for the END of
    #    a big log, get its size from `jobs`/an ls first, then --offset near the end.
    /opt/hermes/.venv/bin/python /opt/alcf/alcf_facility.py output \
        --path /home/<user>/myjob.out --lines 40
    /opt/hermes/.venv/bin/python /opt/alcf/alcf_facility.py output \
        --storage eagle --path /eagle/<project>/run/out.log --offset 500000 --size 4000

    # 4. Projects + allocations (node-hours allocated vs used) (auth).
    #    You may be in MANY projects; --project filters (substring on name),
    #    otherwise only the first few are shown (allocations = 1 API call each).
    /opt/hermes/.venv/bin/python /opt/alcf/alcf_facility.py allocations --project <name>
    /opt/hermes/.venv/bin/python /opt/alcf/alcf_facility.py allocations   # first few projects

Every subcommand accepts `--json` to get the raw API payload if the summarized
view is missing a field you need.

## Behavior notes / pitfalls

- **`status` first, no login needed.** For "is Polaris up?" just run `status` —
  it reports each system's live `current_status` (up/down/unknown) plus recent
  events. Don't make the user authenticate for a status question.
- **The other three need the IRI login** (separate from the inference login). If
  the token is missing the helper exits with code 3 and prints the exact
  `docker exec ... alcf_facility_api_globus_token.py authenticate` command — relay
  that to the user; you cannot complete the browser login yourself.
- **This is your FETCH path — pair it with the PBS diagnosis skill.** Use `jobs`
  (or the IRI `job_status` for one job) to PULL the record, then load
  `alcf-pbs-scheduling-and-docs` to interpret `Exit_status` / `run_count` /
  `comment` / the array `[]` tell. You no longer need the user to paste `qstat`.
- **Filesystem reads are Home/Eagle only** and async under the hood; the helper
  polls the task for you. `head`/`view` are implemented; `tail`/`stat`/`checksum`
  and `upload`/`download` are 501 stubs at ALCF — do not rely on them.
- **Per-identity path allowlist.** Reads are restricted to the token owner's own
  paths: the API rejects a path outside `/home/<the-token-user>`, `/eagle`, or
  `/lus/eagle` with an "Input validation error: Path must start with one of…"
  message that names the allowed roots. So use the USER's own username in the
  path (get it from `account/projects` → `user_ids`, or the allowlist error
  itself), not a guessed one.
- **Verified response shapes (2026-07-31, live token):** `jobs` → each item is
  `{"id": "<pbsid>.polaris-…", "status": {"state","exit_code"}}`; `allocations`
  → `{"entries":[{"allocation","usage","unit"}],"capability_uri":".../<resource>"}`;
  `output` (head/view) → text at `result.output.content`; `ls` → entries at
  `result.output` (list). The helper already parses these; `--json` shows raw.
- **Read-only by design.** This skill never submits, cancels, or deletes. For job
  submission use `iri_hello_world.py` / the `alcf-iri-facility-api` skill; those
  are writes and consume the user's allocation — confirm before running them.
- **Response fields can vary.** The summarized output uses best-effort field
  names; if a value shows as blank, re-run with `--json` and read the raw payload.

## See also

- `alcf-iri-facility-api` — the full IRI API (submit/cancel jobs, filesystem
  ops, the reusable `iri_api_client.py` these helpers build on).
- `alcf-pbs-scheduling-and-docs` — interpret the job records this skill fetches.
- `alcf-inference-service` — the chat/inference backend + model discovery.
