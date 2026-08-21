# Capability Expansion — ideas for giving the ALCF Agent more reach

Status: proposal / not yet built. Drafted 2026-07-31 after a full review of the
baked ALCF docs and the live API surface. Grounded in what is *actually
callable* today (verified against the live IRI OpenAPI spec and the no-auth
status endpoints), not aspirational.

## Method

- Enumerated the live **IRI OpenAPI** (`api.alcf.anl.gov/openapi.json`): 42 paths
  across `account/*`, `compute/*`, `filesystem/*` (18 ops), `facility/*`,
  `status/*`, `task/*`.
- Hit the **public no-auth `status/*` + `facility/*`** endpoints live — they
  return real data (9 resources with `current_status`, 100 events, 100
  incidents with maintenance windows).
- Re-read the baked **inference-endpoints** doc: discovery
  (`list-endpoints`, `sophia/models`, `sophia/jobs`), **embeddings**, and a full
  **batch API** (submit ≤150k requests, poll, fetch results) are all documented.
- Cross-checked against what the agent surfaces today (3 skills:
  inference-service, iri-facility-api, pbs-scheduling; MEMORY.md; one-shot
  `iri_hello_world.py`).

## What the agent already does

Chat on ALCF inference; diagnose PBS scheduling from pasted records; submit a
hello-world job via IRI; read Home/Eagle files (`ls`/`view`/`head`); switch
models. It knows the docs. It is essentially a **chat + hello-world + advisor**.

## The gap in one sentence

The agent can *talk about* ALCF expertly but only *acts* through a thin slice of
IRI (compute submit + a few filesystem reads). Meanwhile the IRI + inference
APIs expose a much larger, already-authorized action surface it doesn't touch.

---

## Proposed capabilities, ranked by (user value ÷ effort)

### TIER 1 — high value, low effort, no new trust boundary

**1. Live system status & outage awareness (NO auth required).**
`GET status/resources` → each system's `current_status` (`up`/`down`);
`status/incidents` + `status/events` → maintenance windows with expected end
times, linked to resources. Lets the agent answer "Is Polaris up right now?",
"When does the current Aurora maintenance end?", "Any ongoing outages?" — the
single most common quick question, and it needs **no token at all** (works even
before the user authenticates). Ship as a small skill + a helper
(`alcf_status.py`) + a MEMORY.md pointer.
*Effort: small. Risk: none (read-only, public).*

**2. "Check my jobs" — real status, not just submit.**
`GET compute/status/{resource}/{job_id}?historical=true` for one job;
`POST compute/status/{resource}` (offset/limit/historical/include_spec) for the
user's whole job list. Turn the raw record into the diagnosis the
`pbs-scheduling` skill already knows how to give — but now the agent *fetches*
the record itself instead of asking the user to paste `qstat -f`. This directly
closes the #1 support workflow gap.
*Effort: small–medium (helper + wire into the PBS skill). Risk: read-only.*

**3. "Get my job's output" — fetch stdout/stderr.**
Compose `ls` (for the file + size) → `view`/`head` on the `.out`/`.err` on
Home/Eagle. A guided `fetch_job_output.py` (job_id → find its output paths →
return the tail). NOTE: `tail` is a **501 stub** at ALCF, so "last N lines" =
`ls` for size then `view` from an offset. VERIFY the current stub set live
before building — the OpenAPI lists all 18 fs ops but ALCF has historically
stubbed several (see Tier-3 note).
*Effort: medium. Risk: read-only.*

**4. Allocation / quota status.**
`account/projects` → `project_allocations` → `user_allocations` gives node-hours
allocated vs. used per project. "How many hours do I have left on <project>?
Am I about to run out?" A `my_allocations.py` helper + skill note.
*Effort: small. Risk: read-only.*

### TIER 2 — high value, medium effort

**5. A real job-submission builder (bounded, not "install my software").**
Not a magic "run my code" button — the agent can't install software (see
constraints). But it CAN turn a described job (executable already on the
system / a module-loaded command + nodes + walltime + queue + filesystems) into
a valid IRI `compute/job` body, submit, poll, and fetch output — baking in the
gotchas the PBS+IRI skills already document (`duration` = integer seconds;
stdout on Home/Eagle only; account/queue lookup from `account/projects`).
Generalizes `iri_hello_world.py` into `submit_job.py`.
*Effort: medium. Risk: WRITE — submitting jobs consumes allocation. Gate behind
an explicit confirm; the DISCLAIMER already covers responsibility.*

**6. Inference power-user features: discovery, embeddings, batch.**
- **Discovery as a first-class action**: "what models are available / hot right
  now?" via `list-endpoints` + `sophia/jobs` (partly done ad-hoc; make it a
  clean helper).
- **Embeddings**: `/embeddings` on Sophia — let the agent embed text/build a
  quick semantic index for a user's files.
- **Batch**: the batch API takes ≤150k requests, writes results to the user's
  Eagle path. The agent could help *construct*, *submit*, and *track* a batch
  job (huge for "score this whole dataset with an LLM"). Results land on Eagle,
  which the agent can then read via IRI filesystem. This is a genuinely
  differentiated HPC-scale capability.
*Effort: medium (embeddings small; batch medium). Risk: batch consumes compute.*

**7. Job lifecycle management: cancel + update.**
`DELETE compute/cancel/{resource}/{job_id}` and `PUT compute/job/.../{job_id}`
(Update Job — e.g. qalter-style changes). "Cancel job 7302913", "bump the
walltime". 
*Effort: small. Risk: WRITE/destructive — confirm before cancel; async 204 ≠
done, so poll.*

### TIER 3 — situational / verify-first

**8. Filesystem write ops (mkdir/mv/cp/rm/chmod/chown/compress/extract).**
The spec lists all of these, but ALCF has **stubbed several with HTTP 501**
(verified earlier: `stat`, `tail`, `checksum` stubbed; `cp`/`mv`/`symlink`/
`compress`/`extract`/`upload`/`download` untested/likely stubbed). Any write
feature must **probe the live endpoint first** and degrade gracefully. `mkdir`
and `rm` are known-working. Don't build on a spec route without a live 200.
*Effort: low each, but gated on live availability. Risk: destructive (rm).*

**9. Optional read-only SSH bridge (BIG trust decision — flag, don't default).**
The one thing IRI can't give: running `qstat`/`module avail`/`myquota`/reading
another-attempt log on a login node. An **opt-in** mount of the user's SSH agent
+ a restricted allowlist of read-only commands would unlock true environment
introspection. This is a real security boundary change and should be a
deliberate, documented, default-OFF opt-in — not something we slip in.
*Effort: medium. Risk: HIGH (ambient cluster access). Needs explicit design +
user buy-in.*

---

## Hard constraints that bound all of the above (don't fight these)

- **No software install / no file staging via IRI.** `upload`/`download` are
  501 stubs at ALCF. The agent cannot push a dataset or binary to a cluster.
  Container/software help stays **advisory** (Apptainer workflow: build→push
  registry→`apptainer pull` on ALCF→run `.sif`; Polaris compute needs
  `--fakeroot`).
  *[2026-08-20: PARTLY STALE — this predates remote-bash (commit 31d9903),
  which gives arbitrary compute-node bash with Home/Eagle mounted and the ALCF
  HTTP proxy, so the agent CAN now stage code/small payloads (`git clone`,
  `pip install`, `printf > file`, `apptainer pull`). Still binds for large
  data, which needs Globus Transfer. See REMOTE-AGENT-ARCHITECTURE.md §1.]*
- **No login-node shell** unless we build the opt-in SSH bridge (Tier 3.9).
- **Filesystem ops are Home/Eagle only** (Polaris fs endpoints 501); async
  (submit→poll `/task/{id}`; a 200 on submit is not success).
- **Two separate Globus logins** (inference ≠ IRI); status endpoints need
  neither.
- **Writes cost the user** (allocation, data). Gate every write/destructive
  action behind confirmation; the DISCLAIMER assigns responsibility to the user.

## Suggested first slice (a coherent "act on my work" release)

Tier-1 bundle: **status/outages (1) + check-my-jobs (2) + get-my-output (3) +
allocations (4)**. All read-only, no new trust boundary, and together they turn
the agent from "chat about ALCF" into "tell me the state of MY work on ALCF."
Package as one new skill (`alcf-facility-status-and-jobs`) + a few small baked
helpers, following the existing `iri_api_client.py` pattern (with the Cloudflare
User-Agent). Then Tier-2 (submit-builder, batch, cancel) as the next release.
