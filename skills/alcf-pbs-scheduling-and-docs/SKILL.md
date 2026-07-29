---
name: alcf-pbs-scheduling-and-docs
description: Diagnose ALCF PBS jobs from the USER side (Polaris/Aurora) — both why a job WON'T run (routing queues, per-queue running+queued limits, job-array pitfalls, held/never-routing jobs) AND why a job FAILED/terminated (state F, Exit_status decode incl. -3 launch-failure and -29 walltime, why run_count is high even when Rerunable=False, system-vs-user attribution, access walls on other users' logs). Also review/edit the ALCF user-guides docs (github.com/argonne-lcf/user-guides). Load when asked "why won't this job run", "what happened to this job", to interpret a qstat/job-detail record or screenshot, to reason about ALCF queue policy, or to audit the ALCF PBS user documentation. DIFFERENT from pbs-monitor (the monitoring TOOLKIT codebase) and the ALCF IRI API skills.
---

# ALCF PBS Scheduling Behavior & User Docs

Two related things this skill covers, both from the *user-support / scheduling-behavior* angle (NOT the pbs-monitor toolkit codebase, NOT the IRI REST API):

1. Diagnosing why a real user's PBS job on Polaris/Aurora is stuck / will never run.
2. Diagnosing why a real user's PBS job FAILED/terminated (state `F`) — see the "Diagnosing a FAILED/TERMINATED job" section below and `references/pbs-exit-status-codes.md`.
3. Reviewing and improving the ALCF user-facing PBS documentation.

## The prod routing-queue model (Polaris)

`prod` is a **routing queue**, not an execution queue. It routes each job by size/walltime into one of the execution queues: `small`, `medium`, `large` (or backfill variants). Key user-facing limit:

- **You may have at most 10 jobs in the running-or-queued state across `small` + `medium` + `large` combined.** `prod` can only route up to 10 jobs into those execution queues at once. (Aurora also has a prod routing queue routing to small/medium/large, but its execution-queue topology differs — do not assume Polaris numbers transfer; verify.)
- debug queue array/job limit = 1; preemptable = 20; **prod = 10** (per the ALCF docs).

## The job-array dead-end (the headline pitfall)

**An un-throttled PBS job array with more than 10 subjobs submitted to `prod` will NEVER run.** It sits in the routing queue forever, accruing eligible time but never being placed, because routing its subjobs would exceed the 10-job small/medium/large cap. PBS *allows* the submission (up to 100 subjobs) without error, so it silently dead-ends. The ALCF docs explicitly call this "a known issue on Polaris."

**Diagnostic tells (from a `qstat -f` output or a job-detail screenshot):**
- Job ID ends in `[]` — e.g. `6967528[]` — that means it is a **job array**, and each *eligible* subjob counts against the 10-job cap.
- Queue = `prod`.
- Huge accrued **eligible time** (e.g. ~2900+ hours ≈ 120+ days) with the job never having run — confirms a long-term structural stall, not transient queue contention.
- The `comment` field (shown by `qstat -was1 <jobid>` or `qstat -f`) usually states the real reason; screenshots often crop it out, so ask for the comment if you can't see it.

**The fix — throttle the array with `%N`:**
```bash
#PBS -J 0-99%10        # at most 10 subjobs eligible at once
qsub -J 0-99%10 sweep.sh
qalter -J 0-99%10 <jobid>[]   # adjust an already-submitted array
```
`%N` (num_concurrent) caps how many subjobs are eligible (queued/running) at a time; the rest sit HELD and don't count against the cap. Set N ≤ 10, and lower (e.g. `%8`) to leave headroom if the project runs other jobs against the same budget.

**IMPORTANT caveat to verify, don't assume:** whether `%N` actually rescues a `prod` array depends on the exact limit semantics. The ALCF docs' phrasing "will not route to an execution queue" hints the routing cap may bite regardless of `%N`. Before recommending `%N` as *the* fix, confirm against the live queue config:
- `qmgr -c "print queue small"` (and medium/large) → look for `max_queued`, `max_queued_res`, `max_run`, `max_run_res`, and whether they're scoped per-user (`[u:PBS_GENERIC]`) or per-project (`[p:...]`).
- If the cap is `max_queued` counting Q+R+H, then HELD subjobs still count and `%N` alone won't help — the honest advice becomes "don't use arrays >10 in prod; use `preemptable`, or a workflow tool like Balsam."
- If the cap is per-**project**, two users in the same project (or two arrays from one user) share the 10 slots and can starve each other even when each is individually throttled — they must coordinate.

## Diagnosing a SINGLE stuck/queued job (not an array): use the WFP score

Not every job sitting in `prod` is structurally stuck. If the job ID has **no `[]`**, the array dead-end does NOT apply — it's a normal single job and the question is priority/backlog, not routing. The most useful field for this is the **WFP score (Score / \"worth-following-priority\")** shown on the job card:

- On Polaris `prod`, eligible jobs are ordered by WFP score, which climbs with queue wait time. A **low** score means low accumulated priority — the job is simply waiting behind higher-scoring work on a busy machine. That is a normal queued job, not a stuck one.
- Calibrate by comparison: in one real case a recently-submitted 32-node/6h single job showed score **~133k**, while two long-stalled array jobs showed **~10.6–10.8M** (≈80× higher). The low-score single job was just backlogged; it did route to an execution queue and was waiting its turn. Don't diagnose \"stuck\" from queue-membership alone — check the score and whether it actually routed.
- Verify: `qstat -f <jobid> | grep -Ei 'comment|job_state|estimated|queue'`, `qstat -T <jobid>` (estimated start), `qstat -u <user>` (what else the project has in flight against the shared cap), `pbs_rstat` (reservations blocking nodes). The `comment` field is ground truth — screenshots usually crop it out, so ask for it.
- Rule out the other single-job never-runs causes too: routing mismatch (walltime/nodes fit no execution queue → \"job disappeared\"), per-project/user Q+R cap already full, or an exhausted/expired allocation (INCITE/ALCC). A low positive score points to plain backlog rather than a hold.

## Diagnosing a FAILED/TERMINATED job (state `F`): run_count, Rerunable, and exit codes

This is the flip side of "why won't it run" — a job that already ran (or tried to) and ended in `job_state = F`. The three fields that crack it are **`Exit_status`**, **`run_count`**, and the **`comment`**. See `references/pbs-exit-status-codes.md` for the exit-code table.

**The headline, counter-intuitive fact — `Rerunable = False` does NOT prevent requeues.** Users (and your own first instinct) assume `Rerunable: False` means "runs at most once." It does not. `Rerunable` governs only whether PBS may requeue-and-rerun a job **that has already begun executing** (e.g. after a node failure or preemption). It says nothing about a job that **fails at/near launch** — PBS's launch-failure requeue path re-dispatches those **independently of `Rerunable`**, up to a server run-count limit (commonly ~20–21). So a **high `run_count` on a `Rerunable=False` job is normal and expected** when early attempts died during startup.

**Therefore `run_count` ≠ "killed N times."** It is (failed/requeued launch attempts) + (the final attempt). `Exit_status` and `resources_used` reflect **only the last attempt**, not the earlier ones. Reconstruct the last attempt from `stime → obittime`:
- `stime→obittime` ≈ a few **seconds** → the job died at launch/prologue every time; the whole run_count is launch-failure requeues. Look for `Exit_status = -3` and `comment: job held, too many failed attempts to run and terminated` + `Hold_Types = s` (system hold placed after exceeding the run-count limit).
- `stime→obittime` ≈ **requested walltime + grace** (e.g. req 30:00, ran 30:41) → the LAST attempt launched cleanly and ran to the wall; `Exit_status = -29` (walltime exceeded), `substate = 93`. The earlier run_count entries are still launch-failure requeues, but the *final* fate is a genuine walltime overrun. Confirm with `resources_used.walltime` slightly exceeding `Resource_List.walltime`, and non-trivial `resources_used.cpupercent`/`cput`/`ncpus` proving it was really computing (not a launch failure).

**Attribution — system-side vs user-side (do NOT jump to "system fault"):**
- `-3` / 3-second deaths / launch failure = *ambiguous*. Could be system (bad node in the `select`, stale filesystem mount, prologue failure) OR user (script errors instantly, `set -e` trips on line 1, output/workdir path unwritable/nonexistent, bad module load). **You cannot tell which from the qstat record alone.** The proof lives in the user's job stdout/err log and the root-only mom log. Do not tell the user "this is ALCF's fault" until one of those is read.
- `-29` walltime = **user-side sizing**, essentially always. The job genuinely needed more than its requested walltime at that scale. Fix: raise `walltime` (respect queue max) or checkpoint to span multiple jobs. No ALCF ticket warranted.

**Escalation order for a launch-failure (`-3`) job:** (1) job OWNER checks their own `Output_Path`/`Error_Path` files — a script traceback ⇒ user-side, empty ⇒ pre-exec/system-ward; (2) only if empty, an ALCF ticket asking ops for the **mom-log launch-failure reason** for the specific job id + timestamp window (you/the owner generally can't read mom logs yourselves — see access walls below).

### Access walls when investigating someone else's job (very common)
You will usually be logged in as a **different user than the job owner**, and hit hard permission walls. Confirm and communicate these rather than flailing:
- **Project data dirs are `drwxrws--- root <PROJECT>` (mode 0770).** If your account isn't in that project's Unix group (`id -Gn | tr ' ' '\n' | grep -i <project>`), you get **Permission denied** on the RUN_ROOT, `logs/`, the `.pbs` script, and the `.o`/`.e` files. Only project members (i.e. the owner) can read them. `/eagle/projects/<P>` on Polaris, `/lus/flare/projects/<P>` on Aurora.
- **PBS server/mom/sched logs are root-only** (`$PBS_HOME=/var/spool/pbs`, subdirs `root:root`). `tracejob <jobid>` reads them, so as an ordinary user it returns **"Couldn't find Job Id ... in logs"** even for a real job — that's a privilege wall, not proof the job doesn't exist.
- **What you CAN get as any user:** `qstat -x -f <jobid>` (finished-job history — the terminal record, incl. `Exit_status`, `run_count`, `comment`, `resources_used`, `exec_host`). This is your primary evidence for a failed job.
- **Jobs live on the cluster they ran on.** Empty `qstat -x -f` on Polaris for an ~8.7M-range id ⇒ try Aurora (`ssh aurora ...`). Match the id family to the right cluster.

## Interpreting job-detail screenshots

When handed a screenshot of a PBS job (e.g. the pbs-monitor web UI job card), extract: job ID (watch for the `[]` array marker), queue, owner, project, nodes/select, walltime, submitted time, and eligible time. If `vision_analyze` fails (the aux vision backend has timed out / returned 500 in the past), fall back to local OCR: `tesseract <image.png> <outbase> --psm 6` then read the `.txt`. tesseract is installed at `/usr/local/bin/tesseract` on this host and reads these UI screenshots cleanly. The `[]` suffix and the eligible-time value are the two fields that most often crack the diagnosis.

## The ALCF user-guides documentation repo

Repo: `https://github.com/argonne-lcf/user-guides` (MkDocs). Clone shallow to review: `git clone --depth 1 ...`.

- **Job arrays are documented in exactly ONE place:** `docs/running-jobs/example-job-scripts.md` — sections "Job array example", "Job array submission scripts", "Interacting with job arrays", and "Limits on job arrays" (the last one documents the prod=10 / arrays-never-route known issue accurately).
- **PBS troubleshooting page:** `docs/running-jobs/known-issues.md` ("Common PBS Issues & Troubleshooting") — covers "Job violates queue/server resource limits", the "job disappeared after submission" routing-mismatch bug, per-user limit errors, common `qstat` comments. It historically did NOT mention the job-array-never-routes failure; a PR was submitted upstream to close that gap (added a "Job Array Submitted to prod Never Runs" section here plus reworked the `example-job-scripts.md` limit note into a `!!! warning` linking the array-size problem to the `%num_concurrent` fix). If reviewing again, check whether that PR merged before re-flagging it as a gap.
- **Nav:** the general "Running Jobs with PBS" section (in `mkdocs.yml`) contains `running-jobs/index.md`, Example Job Scripts, Common PBS Issues, Machine Reservations. It's a cross-machine section, not under a specific machine.
- **Per-machine queue docs:** `polaris/running-jobs/index.md#queues`, `aurora/running-jobs-aurora.md#queues`, `sophia/queueing-and-running-jobs/running-jobs.md#queues`.
- The repo uses `!!! warning` / `!!! tip` admonition boxes — the right vehicle for elevating a buried footgun.

## Doc-review workflow that worked

1. Shallow-clone the repo.
2. `search_files` for `job.?array`, `-J `, `PBS_ARRAY_INDEX`, `num_concurrent`, `subjob` across `docs/` to find every mention (there are few).
3. Cross-check the troubleshooting page and per-machine queue pages for coverage gaps.
4. Report: what the docs DO say (quote verbatim), whether it's discoverable (placement, is it a warning box, does it link problem→fix), and machine coverage (Polaris vs Aurora/Crux/Sophia).
5. When gaps exist, offer a concrete PR: add the failure to `known-issues.md`, convert buried prose to a `!!! warning`, link the array-size problem to the `%num_concurrent` fix — but VERIFY the `%N`-actually-works question against live config before documenting a fix.

## Pitfalls
- **`Rerunable = False` does NOT mean "runs once."** It only blocks requeue of an *already-running* job. Launch-failure requeues ignore it, so a high `run_count` on a non-rerunable job is normal. `run_count` ≠ times killed; `Exit_status`/`resources_used` reflect only the LAST attempt. See the FAILED-job section + `references/pbs-exit-status-codes.md`.
- **Don't jump to "system fault" on a launch failure (`-3`).** It's indistinguishable from a user script that errors instantly, until you read the owner's `.o/.e` log or the root-only mom log. `-29` walltime IS user-side sizing.
- **You usually can't read another user's job logs** — project dirs are 0770 group-owned; PBS server/mom logs are root-only and `tracejob` returns empty without privilege. Confirm the access wall, then route to the owner / an ALCF ticket rather than flailing.
- **The `[]` in a job ID is the single most important tell** — it means a job array, and un-throttled arrays >10 in prod are the classic never-runs failure. Always check for it first.
- **Array size alone is not the killer — the count of *eligible* (Q+R) subjobs is.** An array of any size flows through fine IF throttled with `%N` (N ≤ available slots) AND the limit counts only eligible (not held) subjobs. Don't tell a user "arrays >10 never work" flatly; tell them the throttle and the caveat.
- **Don't assume the 10-job cap's scope.** Per-user vs per-project changes the advice materially. Read `qmgr -c "print queue ..."` rather than guessing.
- **Aurora ≠ Polaris.** Aurora has a prod routing queue too but different execution queues/limits; the docs' array-limit numbers are stated as Polaris-specific and are silent on Aurora. Verify per cluster.
- **`vision_analyze` aux backend can time out (HTTP 500 "streaming required / >10 min").** Don't loop on it — fall back to `tesseract` OCR for text-heavy screenshots.
