# PBS `Exit_status` codes for diagnosing failed (state `F`) jobs

`Exit_status` in a `qstat -x -f <jobid>` record reflects **only the last run attempt**, not earlier requeued attempts. Positive values (0–255) are the user script's own exit code (128+N ⇒ killed by signal N). **Negative values are PBS-generated** — the ones you need for failure diagnosis:

| Code | Symbol | Meaning | Typical attribution |
|------|--------|---------|---------------------|
| `0`  | —      | Script exited 0 (success) | — |
| `1`–`255` | — | Script's own exit code; 128+N = killed by signal N | user script |
| `-3` | JOB_EXEC_RETRY / fail-before-files | Job failed at/near LAUNCH before output files staged; PBS may requeue (independent of `Rerunable`) | **AMBIGUOUS**: bad node / stale FS mount / prologue (system) OR script errors instantly / unwritable output path / bad module load (user). Needs owner's `.o/.e` log or root mom log to disambiguate. |
| `-11` | JOB_EXEC_BADRESRT | Bad restart | system |
| `-29` | JOB_EXEC_KILL_WALLTIME | Killed for exceeding requested walltime | **user sizing** — raise `walltime` or checkpoint |
| other negatives | JOB_EXEC_* | Various exec/prologue/epilogue failures | check PBS source `job.h` / mom logs |

## Corroborating fields (all in `qstat -x -f`)
- `run_count` — total dispatch attempts. **High value on a `Rerunable=False` job = launch-failure requeues, NOT reruns of a running job.** Not "killed N times."
- `comment` — often ground truth. `job held, too many failed attempts to run and terminated` = hit the run-count limit after repeated launch failures. `Job run at <time> on (<node>...)` = names the last run's placement.
- `Hold_Types = s` — **system hold** auto-placed after too many failed launch attempts.
- `substate = 91` — terminated after failed-attempt limit. `substate = 93` — terminated by walltime enforcement.
- `stime` → `obittime` — wall-clock span of the LAST attempt. ~seconds ⇒ launch failure; ~(requested walltime + ~5min grace) ⇒ ran to the wall.
- `resources_used.{walltime,cput,cpupercent,ncpus,mem}` — proves the last attempt actually computed. Present + large ⇒ real run (so `-29` is a true overrun); absent/tiny ⇒ never really ran.

## Worked examples (from real ALCF jobs, 2026-07)
- **`c2a04rfd3smk` (7297144, Polaris):** `Rerunable=False`, `run_count=21`, `Exit_status=-3`, `stime→obittime`=3s, `Hold_Types=s`, `comment=job held, too many failed attempts...`. ⇒ 21 launch-failure requeues, never ran; ambiguous system-vs-user; needs owner log / mom log.
- **`ITER-15MA-DD_scale_4096` (8702499, Aurora):** `Rerunable=False`, `run_count=15`, `Exit_status=-29`, req `walltime 00:30:00`, `resources_used.walltime 00:30:41`, `substate=93`, `resources_used.cput 21789h` across `851968` ncpus. ⇒ last attempt ran full 30 min on 4096 nodes then walltime-killed (user sizing); the other 14 counts were earlier launch-failure requeues. Same pattern for `_scale_1024` (8702497): 1024 nodes, `walltime 00:30:31` vs req `00:30:00`, `-29`.
