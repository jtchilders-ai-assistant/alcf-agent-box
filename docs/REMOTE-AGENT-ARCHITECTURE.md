# Remote agent on ALCF compute nodes — development paths

Status: proposal / not yet built. Drafted 2026-08-20 after a review of the
current repo against the live ALCF service surface. Grounded in what the box can
*already do today* (verified in prior spikes and recorded in `DESIGN.md`,
`CAPABILITY-IDEAS.md`, and the skills) plus the ALCF Globus Compute docs. Every
claim below is marked **verified**, **documented** (ALCF says so, we haven't run
it), or **unknown — needs a spike**.

## The ask

> The laptop agent submits a job to Polaris that launches *another* agent on the
> compute node, which executes a task packaged with it or sent along. The laptop
> agent can also talk to the compute-node agent.

## Method

- Re-read every helper in `scripts/` for what is actually callable today.
- Cross-checked the ALCF Globus Compute docs page (not in the baked snapshot)
  for MEP configuration knobs and single-user-endpoint policy.
- Re-checked the Polaris/Aurora proxy stanza, because compute-node egress is the
  hinge that decides whether a node-side agent can have an LLM brain at all.
- Audited the laptop→cluster and cluster→laptop directions separately, because
  they turn out to be wildly asymmetric, and that asymmetry should drive the
  protocol design.

---

## 0. The actual motivation — context and tool shape, not distribution

The driving problem is not "I want work distributed across machines." It is:

- the open models backing the agent have **small context windows** (Sophia serves
  `gpt-oss-120b` at 65 536 and the default `gemma-4-31B-it` at 128 000), and
- they are **trained on a direct `bash` tool**, not on
  `python /opt/alcf/alcf_remote_bash.py run --account X --cmd "..."`, so they make
  tool-call mistakes;
- build/test loops are the most context-hungry and most error-prone workload.

That diagnosis is right, and a build/test worker agent is the right *shape* of
answer. But the compute node is the **expensive place** to get it, and there are
three cheaper fixes that address the same root causes. Two of them are already
supported by Hermes and simply unused by this repo.

### 0.1 The bug: `remote_bash` has no shell state, and the skill says it does

`remote_bash()` runs every command as a **fresh** `subprocess.run(["bash","-lc", cmd], cwd=workdir)`
(`scripts/alcf_remote_bash.py:161`). Consequences, per call:

| State | Persists across calls? |
|---|---|
| Files written to Home/Eagle | **yes** |
| `--venv` activation | **yes** (re-sourced each call, line 155) |
| `--run-dir` as starting cwd | **yes** (re-applied each call) |
| A `cd` *inside* a command | **no** |
| `export FOO=bar` | **no** |
| **`module load X`** | **no** |

So the `batch` example shipped in `skills/alcf-remote-bash/SKILL.md:102-105`—

```
--cmd 'module load spack-pe-base cmake'
--cmd 'cmake -B build'
--cmd 'cmake --build build -j'
```

—**cannot work as written.** The `module load` evaporates when command 1's shell
exits; commands 2 and 3 get `cmake: command not found`, which is precisely the
failure the skill warns about two sections later ("`module load` is needed …
they are NOT on the default PATH"). The same file also states that in a batch
"files/**vars** a step creates are visible to later steps" — true for files,
false for shell variables and module state.

This matters more than the wrapper syntax. A model trained on bash *assumes*
`cd`, `export`, and `module load` persist — that assumption is baked into how it
sequences commands. The skill currently documents the broken pattern, so the
model is being actively taught to do the thing that fails, then blamed for the
resulting mistakes. **Fix this before concluding the models are the problem.**

Two fixes, in order of ambition: (a) client-side emulation — track cwd/env and
prepend `cd <tracked> &&`, capturing the resulting state after each command; or
(b) a genuinely persistent shell process on the node that commands are fed to.
(b) is strictly better and is one of the real arguments for a node-side worker.

### 0.2 The output cap is 40× the context window

`_RESULT_LIMIT` is 9 500 000 bytes (`alcf_remote_bash.py:106`) — roughly 2.4 M
tokens against a 64 k window. A single chatty `cmake --build` can therefore blow
the entire context in one tool result. Default should be head+tail of ~100 lines
each, full log written to a file on the cluster, plus a `grep`-the-log
affordance so the agent pulls only the lines it needs. Small change, large effect
on a 64 k model.

### 0.3 Hermes already has the two mechanisms needed — this repo uses neither

From the Hermes docs:

- **MCP servers** are configured with an `mcp_servers:` block (`command`, `args`,
  `tools: include:`). Wrapping remote-bash in a tiny stdio MCP server that
  exposes a single tool named `bash` with one parameter `command` gives the model
  the **exact call shape it was trained on**. `--account`, `--queue`,
  `--walltime`, `--endpoint` get bound in the server's closure and become
  invisible to the model — removing both the unfamiliar syntax and a whole class
  of nested-quoting errors.
- **`delegate_task`** spawns real subagents with their own fresh context, and
  `delegation.provider` / `delegation.model` route them to a **different model**
  than the main conversation. The docs explicitly recommend delegation over
  `/model` switching on long sessions for exactly this reason.

That second point is the context firewall the worker-agent idea is reaching for,
available today at config level. And it means the build worker **does not have to
use the main agent's model**: it wants long context and good tool-calling, not
conversational polish. Against the current fallback list, `gemma-4-26B-A4B-it`
and `nemotron-3-super-120b` both serve at **262 144** on Sophia — 2× the default
and 4× `gpt-oss-120b`. (`gpt-oss-120b` is also 131 072 on **Metis** vs 65 536 on
Sophia; if that's the model in use, the cluster choice alone doubles the window.)

### 0.4 What a node-side worker still uniquely buys you

After 0.1–0.3, the remaining honest advantages of putting the worker *on the
node* are:

- **A real persistent shell.** Not emulated — an actual long-lived process where
  `cd`, `export`, and `module load` behave as the model expects.
- **No per-command RPC.** ~1 s warm × 50 commands ≈ a minute of pure overhead per
  build loop; on the node it's ~0.
- **No `bash -l` re-init per command.** Currently every single call re-runs the
  full login profile, including module init.
- **Durability.** Long builds and overnight work survive a closed laptop.

Those are real. They are also the *second-order* wins, available once the
first-order ones are collected — and moving a working subagent onto a node is a
placement change, not a new architecture.

---

## 1. What the box can already do (capability inventory)

This is the part most likely to be under-appreciated: **most of the transport
already exists.** What's missing is a protocol and a node-side runtime, not
plumbing.

| Capability | Mechanism | Cost | Status |
|---|---|---|---|
| Run a shell command on a Polaris compute node | Globus Compute MEP (`alcf_remote_bash.py run`) | ~60 s cold, ~1 s warm; holds a node | **verified** 2026-08-04 |
| Many commands on one warm node | `alcf_remote_bash.py batch` (one `Executor` session) | 20.9 s cold → 1.0 s warm, same node | **verified** |
| **Write a file onto Home/Eagle from the laptop** | `remote_bash --cmd "printf ... > f"` / base64 | one Globus Compute call | **verified** (implied by `batch` pitfall notes) |
| Pull code/deps onto the cluster | `git clone`, `pip install`, `apptainer pull` on the node through `proxy.alcf.anl.gov:3128` | one call | **documented** + apptainer path verified |
| Submit a real PBS job | IRI `POST /compute/job/{polaris}` (`iri_hello_world.py`) | free API call | **verified** 2026-07-30 |
| Poll job state | IRI `GET /compute/status/...` (`alcf_facility.py jobs`) | free | **verified** |
| Cancel a job | IRI `DELETE /compute/cancel/...` | free | documented, no helper yet |
| **Read a growing file incrementally** | IRI `view` with `--offset` (`alcf_facility.py output --offset N`) | free, no allocation | **verified** (built for the no-`tail` workaround) |
| `mkdir` on Home/Eagle | IRI `filesystem/mkdir` | free | **verified** (returns 201) |
| One Globus consent for inference + IRI + Compute | `alcf_combined_auth.py` | — | **verified** (commit `a78f477`) |
| Multi-node blocks | `nodes_per_block` in `user_endpoint_config` | — | wired in `--nodes`, never exercised |
| `worker_init`, `max_idletime`, `MpiExecLauncher`, `scheduler_options` | MEP `user_endpoint_config` | — | **documented**, **not used by the repo at all** |

### Correction to an existing repo doc

`CAPABILITY-IDEAS.md` lists as a hard constraint:

> **No software install / no file staging via IRI.** … The agent cannot push a
> dataset or binary to a cluster.

That was true when written (it predates remote-bash, commit `31d9903`). It is
**no longer true for code and small payloads**: `remote_bash` gives an arbitrary
`bash -lc` on a compute node with Home/Eagle mounted and an HTTP proxy to the
internet, so the agent can write files, `git clone`, `pip install`, and
`apptainer pull`. The constraint now only binds for **large data**, where you
genuinely need Globus Transfer. That doc should be amended — it is currently
steering the design away from a path that is open.

---

## 2. The three problems that actually need solving

Everything else is detail.

1. **Staging** — get the task (code, inputs, manifest) onto the cluster.
   *Mostly solved.* Small bundles via remote-bash, code via `git clone`,
   environments via `apptainer pull`, large data via Globus Transfer (not yet
   wired — would need the transfer scope added to `alcf_combined_auth.py`).
2. **Control channel** — laptop ⇄ node, across a PBS queue wait, a NAT'd laptop,
   and a compute node that accepts no inbound connections. *This is the hard
   one.* See §3.
3. **Autonomy** — does the node-side process need its own LLM brain, or is it a
   dumb executor driven by the laptop? *This is a choice, not a constraint*, and
   getting it wrong is the main way to over-build v1. See §4.

---

## 3. The channel: the asymmetry that should drive the design

The two directions are **not** symmetric, and this is the single most important
fact for the protocol:

**Node → laptop is free, durable, and easy.**
The node writes to a file on Home/Eagle; the laptop reads it with IRI
`view --offset` — a plain authenticated REST call that consumes no allocation,
needs no live connection, works while the job is queued, and resumes cleanly
after the laptop sleeps. `alcf_facility.py output --offset` already implements
exactly this.

**Laptop → node is expensive and awkward.**
There is no verified write-file API. The options are:

| Option | Cost | Latency | Status |
|---|---|---|---|
| Globus Compute one-liner (`printf > inbox/msg`) | **holds a compute node** | ~1 s warm / ~60 s cold | verified, but you pay node-hours to deliver a message |
| IRI `filesystem/upload` | free | seconds | **unknown — likely a 501 stub.** Spike S2. |
| IRI `mkdir` with the message encoded in the *directory name* | free | seconds | mkdir verified; the encoding trick is untested |
| Globus Transfer HTTPS PUT to an ALCF collection | free | seconds | **unknown.** Spike S5. Needs a new scope. |
| IRI `DELETE /compute/cancel` and `PUT /compute/job` (qalter) | free | seconds | coarse control only — cancel, walltime bump |

The `mkdir`-as-mailbox trick deserves a sentence because it is a genuinely
zero-dependency fallback: `mkdir` is verified working and free, and a path
component holds 255 bytes, so ~190 bytes of base64 payload per numbered
directory (`inbox/0007.<b64>`), chainable for longer messages. It is ugly. It
also works today, with no new API, no new scope, and no node-hours. Keep it in
the back pocket in case S2 and S5 both come back negative.

### The design conclusion

Because outbound is cheap and inbound is expensive, **do not design a chatty
bidirectional agent-to-agent conversation.** Design:

- a **rich, continuous event stream** node → laptop (progress, decisions, logs,
  questions, results), and
- a **sparse, coarse control path** laptop → node (pause, cancel, redirect,
  answer one question, raise a budget).

The node agent must be able to run to completion having **never received a
single inbound message**. Interjections are an optimization, not a requirement.
That property is what makes it survive a 6-hour queue wait and a closed laptop.

### The session lives on the filesystem

Corollary worth stating explicitly: **a directory on Eagle *is* the session
object.** Task manifest, inbox, outbox, event log, artifacts, and a status file
all live under `/eagle/<project>/<user>/.alcf-agent/<session-id>/`. Everything
else — Globus Compute, IRI, the laptop process — is transport over that state.
This gives you resumption for free: a restarted laptop agent re-attaches by
session id and replays the event log from offset 0. Do not put session state in
the laptop's memory.

---

## 4. Autonomy: separate "remote execution context" from "remote agent"

These get conflated, and conflating them is how v1 becomes a six-month project.

- **Remote execution context** = a warm, addressable place on ALCF where work
  happens and state persists. The laptop agent does all the reasoning.
- **Remote autonomy** = an LLM loop *on the node* that can decide things without
  the laptop.

You only need autonomy when the laptop **cannot** be in the loop: overnight runs,
long queue waits, a closed lid, or a sub-second decision cadence the channel
can't support. For "compile this, run it, fix the error," the laptop agent over a
warm node is *better* — it has your full skills, memory, and conversation.

So: build the execution context first, add autonomy only where it earns its keep.

### If you do want a brain on the node, three sources

- **C1 — thin worker agent (recommended).** A single-file Python agent loop:
  OpenAI-compatible client → ALCF inference, a small tool set (bash, file ops,
  `mpiexec`), and the mailbox protocol. Ships inside the task bundle. Gated on
  spike S1 (can the node reach the inference API?).
- **C2 — Hermes on the node.** `apptainer pull` the same image, run it headless.
  Full skill/memory parity, but Hermes is built around an interactive dashboard,
  not headless one-shot batch execution, and you'd be dragging a multi-GB image
  onto Eagle. Only if you truly need skill parity.
- **F — model on the allocated GPUs.** Stand up vLLM on the Polaris node's 4×A100
  and let the node agent's brain run on the node it's working on. No token
  egress, no proxy dependency, no gateway rate limits, and a genuinely
  HPC-native story. Costs GPU share and model-load time. Tier 3, but it is the
  most differentiated version of this idea and worth prototyping eventually.

### The proxy gotcha that decides C1 and F

ALCF's documented `no_proxy` for Polaris is:

```
admin,polaris-adminvm-01,localhost,*.cm.polaris.alcf.anl.gov,polaris-*,*.polaris.alcf.anl.gov,*.alcf.anl.gov
```

That trailing `*.alcf.anl.gov` matches **`inference-api.alcf.anl.gov`** — so a
node-side agent that copies the documented proxy stanza will try to reach the
inference gateway *directly*, and compute nodes have no outbound route. If the
gateway is not internally routable from a compute node, the node agent's LLM
calls fail in a way that looks like a hang or a DNS error, not an auth error.

Fix if confirmed: set `no_proxy` narrowly in `worker_init` so
`inference-api.alcf.anl.gov` goes *through* `proxy.alcf.anl.gov:3128`. This is
cheap to test and it gates the entire node-side-autonomy branch. **Spike S1.**

---

## 5. The paths

### Path A — Warm node as a remote execution context *(smallest useful delta)*

Extend `alcf_remote_bash.py` from "run a command" to "hold a session":

- add `worker_init` (proxy env, venv activation, module loads) and
  `max_idletime` to `_make_uec()` — both are documented MEP knobs the repo
  currently ignores, and `max_idletime` defaults to **240 s**, which means the
  warm block the `batch` docs celebrate silently dies after 4 idle minutes;
- add `put`/`get` file primitives (base64 in, `view` out) so staging is a
  first-class verb instead of a `printf` trick;
- persist the `Executor` across agent turns so a *conversation* keeps one warm
  node, not just a single `batch` invocation;
- add a node-hour budget guard and a kill switch.

No node-side agent, no new protocol, no new trust boundary. The laptop agent
gets a persistent sandbox on Polaris. **Effort: S. Risk: low.** This alone
covers a large fraction of what people actually mean by "let the agent work on
ALCF."

### Path B — Detached job + filesystem mailbox *(the one that matches the ask)*

1. Laptop packs a **task bundle** and stages it to
   `/eagle/<project>/<user>/.alcf-agent/<session>/` (Path A's `put`, or
   `git clone` on the node).
2. Laptop submits a real PBS job — IRI `POST /compute/job` with `executable`
   pointing at the bundle's launcher — so the job has a proper shape (nodes,
   queue, walltime, filesystems) and appears in `qstat` independent of the
   laptop.
3. The launcher starts the node-side runner, which appends JSONL events to
   `events.jsonl` and polls `inbox/`.
4. Laptop watches with IRI `view --offset` (free), controls with the sparse
   channel from §3, cancels with `DELETE /compute/cancel`.

This is the architecture that actually delivers "submit a job that launches an
agent, and talk to it." It survives queue waits and a closed laptop.
**Effort: M. Risk: medium.** The one soft spot is the inbound message path
(§3) — which is exactly what spikes S2/S5 resolve.

### Path C — Node-side LLM loop

Layer C1 (§4) on top of B. Gated on S1. **Effort: M. Risk: medium** — this is
where an agent burns allocation unsupervised, so the budget guard and audit log
in §7 stop being optional.

### Path D — Node-hosted Globus Compute endpoint *(park it)*

The job launcher starts a single-user endpoint inside the allocation and the
laptop submits functions straight to it — elegant, low-latency, NAT-friendly on
both sides, and it reuses the auth the box already has. Two problems: ALCF
documents single-user endpoints on **login or edge service nodes**, not compute
nodes, and the endpoint needs outbound AMQP (5671) from the node, which the HTTP
proxy likely won't carry. Park unless a spike says otherwise.

**The viable variant** is a user-run endpoint on a **login/edge node** with a PBS
provider, long walltime, and a high `max_idletime` — effectively a private MEP,
giving a durable addressable agent host with no new protocol at all. Great for
power users; it breaks the "one `docker run`, no cluster-side setup" promise, so
it's an advanced opt-in, not the default.

### Path E — External rendezvous relay *(the right long-term answer, wrong owner)*

A small broker both sides dial out to over HTTPS. Lowest latency, cleanest
protocol. It also adds infrastructure and a third-party trust boundary, which
directly contradicts the box's "no external service, everything in the
container" design principle.

Worth flagging up the chain instead: **IRI already owns the API surface and the
auth.** An IRI "agent session" endpoint — create a session, stream events, post a
message — would make this whole memo a hundred lines of client code. Since this
repo's author is inside ALCF, that's a conversation worth having rather than a
thing to build locally.

---

## 6. Recommended track

Ordered by benefit ÷ effort against the **actual** problem (§0), which means the
distributed architecture lands *last*, not first. Each layer is independently
shippable and each one makes the next cheaper.

### Layer 0 — fix the tool, not the topology *(hours to days; do this first)*

1. **Correct the shell-state bug and the skill that documents it** (§0.1). At
   minimum, fix the broken `batch` example and the "vars persist" claim today —
   that is a same-day change that stops teaching the model a failing pattern.
   Then add client-side cwd/env tracking.
2. **Cap tool output** (§0.2): head+tail by default, full log on the cluster,
   `grep`-the-log affordance.
3. **Expose remote-bash as an MCP tool named `bash`** (§0.3) taking a single
   `command` parameter, with account/queue/walltime/endpoint bound server-side.
4. **Point `delegation.model` at a long-context model** (§0.3).

This is the highest-leverage work in this document and none of it requires a new
architecture. It is entirely plausible that it resolves most of the observed
error rate on its own — which is worth knowing *before* committing to build a
distributed system.

> **Status 2026-08-20: Layer 0 implemented.**
> 1+2. `alcf_remote_bash.py` — session state (cd/export/`module load` persist
>    via `~/.alcf_remote_bash/<session>.state` on the cluster; `--session` /
>    `--fresh`) and output budget (default 20 kB head+tail, full log spilled on
>    the node, `--max-output` / `--full-output`). SKILL.md corrected.
> 3. `alcf_bash_mcp.py` — dependency-free MCP stdio server, one tool `bash`
>    (optional sticky `account`, `confirm` for the destructive gate), one warm
>    Executor for its lifetime.
> 4. `entrypoint.sh` appends `delegation:` + `mcp_servers:` blocks, each gated
>    on a runtime grep of the installed Hermes source (fail-open if the pinned
>    base predates the feature); `ALCF_DELEGATION_MODEL` defaults to the launch
>    model.
> Covered by `tests/test_layer0.py` (15 tests, local-exec mode — state,
> truncation, destructive gate, full MCP round-trip). NOT yet verified against
> the live MEP or a live Hermes MCP client — that's the first thing to check in
> a real container run.

### Layer 1 — the build/test contract *(days)*

A `build-on-alcf` skill that drives `delegate_task`: the subagent gets the goal,
a success test, the bound `bash` tool, and its own fresh context; the main
conversation gets back a verdict, an artifact path, and a short failure summary
instead of 4 000 lines of compiler output. This is the context firewall, using
Hermes' native mechanism.

Define the contract here — goal / success criteria / artifact / verdict — because
it is what Layers 2 and 3 reuse when the worker's *location* changes.

### Layer 2 — take the LLM out of the noisy loop *(days)*

Most of a build loop is deterministic: detect the build system, configure, build,
test, and classify the common failures (missing module, missing dep, wrong
compiler, OOM, out of disk). A harness that handles those and escalates only the
residual to the LLM is cheaper, more reliable, and burns far less context than an
LLM driving every step. Models are bad at long noisy loops — the fix is to not
put them in one.

### Layer 3 — move the worker onto the node *(the original idea; weeks)*

Now it is a **placement change** to a working subagent, not a new architecture,
and the motivation is specific and defensible (§0.4): a real persistent shell, no
per-command RPC, no `bash -l` re-init, and durability across a closed laptop.

Sequence within this layer: Path A first (`worker_init`, `max_idletime`,
`put`/`get`, persistent session, budget guard, kill switch), then the §8 spikes,
then Path B's session directory and event stream, then C1's node-side brain
behind a flag — deterministic runner first, LLM second.

**The contract, wherever the worker runs:**
- `task.yaml` — goal, success criteria, resource request, entrypoint, inputs,
  budget ceiling;
- `events.jsonl` — append-only typed events (`started`, `progress`, `log`,
  `question`, `artifact`, `decision`, `done`, `failed`);
- `inbox/` — sparse control verbs (`pause`, `resume`, `answer`, `redirect`,
  `cancel`, `raise-budget`).

Design it as if the transport will change, because it will (S2/S5, possibly Path
E later). The contract is the durable asset; the transport is not.

**Repo shape**, following existing conventions: `scripts/alcf_remote_agent.py`
(pack / submit / watch / say / collect / cancel), a `skills/alcf-remote-agent/`
skill, Dockerfile `COPY` lines, a `MEMORY.md` pointer, and a `DESIGN.md` update.

### Do not skip Layer 0 on the way to Layer 3

A node-side worker inherits every Layer 0 defect. If the tool shape is wrong and
the output cap is 40× the window, moving the agent to Polaris just relocates the
problem onto a machine that is harder to debug, costs allocation while it
thinks, and needs a token that expires in 48 hours.

---

## 7. Risks and safety

- **Runaway allocation burn.** An autonomous loop on a compute node spends real
  node-hours. Needs a hard node-hour ceiling in the manifest, enforced *on the
  node* (not just laptop-side), plus PBS walltime as the backstop.
- **The existing runaway failure mode is already documented and already
  manual**: `alcf_remote_bash.py` warns that fail-looping jobs "won't stop on
  their own" and recovery means `rm ~/.globus_compute/*/daemon.pid` **on the
  cluster**. A long-lived agent makes this more likely, not less. A kill switch
  that works from the laptop (IRI `cancel` + a `HALT` sentinel the node polls) is
  a Phase-1 requirement, not a Phase-4 nicety.
- **Warm-node cost.** A held block occupies a whole Polaris node. Path A's
  persistent session must show the user what it's costing and idle out by
  default (`max_idletime` is your friend here, not your enemy).
- **Token handling.** A node-side brain needs an inference token. Prefer passing
  it in the Globus Compute payload / process env over writing it to a shared
  filesystem; if it must be staged, `0600` under the user's own home and delete
  on exit. Note the 48 h expiry — a job that queues for 20 h and runs for 30 h
  will have its token die mid-run. Design for re-issue, or prefer Path F.
- **Result size caps.** Globus Compute caps a result payload at ~10 MB (the
  helper already truncates). Real output goes to a file on Eagle and is read via
  `view --offset`, never returned through the RPC.
- **Auditability.** Everything the remote agent does should be reconstructible
  from `events.jsonl` after the fact. This is what makes the capability
  defensible to ALCF ops.
- **Facility policy.** An autonomous LLM agent taking unsupervised actions on
  compute nodes under a user's allocation is a policy question, not just a
  technical one. `DISCLAIMER.md` assigns responsibility to the user, which is
  necessary but probably not sufficient. Worth raising with ALCF ops *before*
  Phase 3's LLM flag ships, not after.

---

## 8. Spikes to run first

Each is roughly a day and each one kills or confirms a branch.

- **S1 — Can a Polaris compute node reach `inference-api.alcf.anl.gov`?**
  Via `remote_bash`, curl the gateway with (a) the documented proxy stanza
  verbatim, (b) a corrected narrow `no_proxy`, (c) no proxy at all. *Decides
  whether node-side autonomy (C1) is possible at all, and whether Path F stops
  being exotic and starts being necessary.*
- **S2 — Are IRI `filesystem/upload` / `download` live or 501 on Home/Eagle?**
  One probe each. *Decides the inbound control channel: a clean free write path,
  or the `mkdir`/Globus-Compute fallbacks.*
- **S3 — Does a detached process survive a Globus Compute function return?**
  `setsid nohup` a script that writes a heartbeat, return immediately, confirm it
  keeps writing until walltime. Also confirm `max_idletime` extends the warm
  block as documented. *Decides how much of Path B can be driven through the MEP
  instead of IRI `qsub`.*
- **S4 — Does `mpiexec` work from inside a Globus Compute worker?**
  `nodes_per_block=2` + `launcher_type: MpiExecLauncher`, check `$PBS_NODEFILE`
  is populated and a 2-node `mpiexec` runs. *Decides whether the remote agent can
  drive real multi-node science, or only single-node build/test work — i.e.
  whether this is a toy or a tool.*
- **S5 — Globus Transfer as a file channel.** Add the transfer scope to
  `alcf_combined_auth.py`, test HTTPS GET/PUT against the ALCF Eagle collection.
  *Decides large-data staging and, if HTTPS PUT works, gives a clean free
  inbound path that makes S2's answer moot.*

Run S1 and S2 first — between them they determine whether the recommended track
above survives contact.
