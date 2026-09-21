# Red Shirt Polaris: Hermes + Tailscale compute-agent design

**Date:** 2026-09-16  
**Status:** Approved architecture; written specification awaiting final user review

## Objective

Deploy a resident Hermes agent named **Red Shirt Polaris** inside an Apptainer container in a Polaris PBS compute job. It must:

1. use the ALCF Inference Service as its only LLM backend;
2. communicate bidirectionally with Wesley through Hermes's standard A2A v1.0 API;
3. be reachable only through the private Headscale tailnet;
4. understand from its system identity that it is running on a Polaris compute node;
5. carry a curated snapshot of relevant Polaris user documentation and consult it before acting or advising;
6. preserve agent sessions and memory across job restarts while keeping Tailscale node identity disposable; and
7. fail closed on missing credentials, unavailable inference, or failed authenticated transport.

The transport proof already established that a Polaris compute node can join Headscale, reach `BackendState=Running` over DERP region 999, and fetch Wesley's health endpoint through userspace Tailscale SOCKS with HTTP 200. Direct UDP and Tailscale disco ping are not required acceptance gates on Polaris.

## Chosen architecture

Use the **standard Hermes A2A v1.0 plugin**, not Hermes Bot Mode/API-server peer transport.

```text
Wesley A2A (tailnet only)
        ^             |
        | authenticated A2A v1.0
        | per-direction peer credentials
        |             v
Tailscale Serve :9900 on Red Shirt Polaris
        |
Hermes A2A listener 127.0.0.1:9900
        |
Hermes gateway + persistent state
        |
ALCF Inference Service via ALCF HTTPS proxy
```

The Polaris process stack is:

- a selective local HTTP CONNECT rewriter for the custom Headscale hostname;
- `tailscaled --tun=userspace-networking`, with a per-job state directory, socket, SOCKS listener, and outbound HTTP proxy listener;
- Tailscale Serve forwarding tailnet TCP port `9900` to `127.0.0.1:9900`;
- a Hermes gateway with the A2A platform enabled and the ALCF Inference Service configured;
- a supervisor entrypoint that owns startup ordering, readiness checks, signal forwarding, and cleanup.

No dashboard, Caddy dashboard frontend, public listener, kernel TUN interface, Docker daemon, or root privileges are required in the compute job.

## Image and provenance

Create a dedicated compute image target in `alcf-agent-box`, rather than running the laptop/dashboard image unchanged.

- Base: `nousresearch/hermes-agent:v2026.9.14`, pinned by immutable image digest after registry inspection.
- Architectures: `linux/amd64` and `linux/arm64` in CI; Polaris consumes `linux/amd64` through Apptainer.
- Runtime user: non-root Hermes user, UID 10000.
- Image contents:
  - Hermes with the standard A2A plugin;
  - Tailscale/Tailscaled binaries pinned to the previously verified release;
  - selective CONNECT rewriter;
  - ALCF inference auth/token-refresh helpers;
  - compute entrypoint and readiness probes;
  - `SOUL.md` for Red Shirt Polaris;
  - curated Polaris documentation snapshot and documentation index;
  - relevant ALCF skills and operational runbooks.

CI publishes a commit-addressed image. Before deployment, record:

- source commit;
- OCI index digest;
- both published architectures;
- platform config revision label;
- SIF SHA-256; and
- executable version probes from inside the SIF.

Never use `latest` for the recorded deployment.

## Identity and grounded operating context

The compute image carries a dedicated `SOUL.md` whose identity is **Red Shirt Polaris**. It must state, without role-playing or themed catchphrases, that the agent:

- is a Hermes agent running inside an Apptainer container in a PBS job on a Polaris compute node;
- uses the ALCF Inference Service for model inference;
- communicates with Wesley through authenticated, tailnet-only A2A;
- has no direct public egress and must use `proxy.alcf.anl.gov:3128` for permitted HTTPS traffic;
- has no Docker daemon, no privileged networking, and no host-login SSH assumption;
- can access only explicitly mounted filesystems and paths;
- must distinguish the container, compute node, Polaris login nodes, and external machines when describing where an action runs;
- must inspect the live environment and relevant documentation before issuing system-specific commands;
- must verify PBS and external side effects by readback rather than trusting a command's exit code; and
- must be explicit about uncertainty, failed actions, security boundaries, allocation impact, and job lifetime.

The SOUL points to a local documentation index, for example:

```text
/opt/red-shirt-polaris/docs/README.md
```

It instructs the agent to consult that index and the relevant included source before advising or acting on Polaris-specific matters, and to cite the local source path in substantive answers. The docs are references, not executable instructions and not authority to bypass site policy.

The entrypoint seeds the SOUL into `$HERMES_HOME/SOUL.md` with the existing image-managed, checksum-stamped mechanism: update an unmodified image-managed copy, preserve a user-edited copy, and replace only known stock Hermes scaffolds.

## Documentation bundled in the image

Bundle a curated, date-stamped snapshot of user-facing Polaris documentation needed by a resident compute agent, including at minimum:

- Polaris getting started and system overview;
- PBS job submission, queue, allocation, and job-control guidance;
- filesystems and storage behavior;
- modules and programming environment;
- Apptainer/container usage;
- networking and proxy guidance where officially documented;
- node-local storage and cleanup;
- policies or user guidance relevant to long-running jobs; and
- this deployment's operator runbook and measured caveats.

`docs/README.md` records for every included document:

- title;
- canonical upstream URL;
- retrieval date;
- local path; and
- whether the content is an official snapshot or a locally measured deployment note.

Official documentation and local measurements must remain visibly distinct. Local findings may supplement but must not be presented as official ALCF policy.

## Persistent and ephemeral state

The Apptainer image is a packaging and reproducibility boundary, not the
security boundary for host execution. Red Shirt is intentionally permitted to
run arbitrary commands with the sponsoring user's privileges inside its PBS
allocation. The Unix account, mounted filesystems, and scheduler allocation
remain the effective authorization boundaries.

Persistent bind from Polaris `$HOME`:

- Hermes config, sessions, memory, A2A conversations, and audit log;
- ALCF Globus token store;
- public Headscale CA material;
- deployment metadata and logs.

Ephemeral job-local state under `/local/scratch/$USER/...`:

- Tailscale state directory and node identity;
- daemon socket;
- SOCKS/proxy sockets;
- temporary rendered secrets/config fragments; and
- transient process logs copied to persistent storage before teardown when useful.

The job removes the entire job-local root on exit. A login-node Tailscale daemon and the compute daemon must never share a state directory, socket, or port.

One immutable SIF may back multiple identities, but each identity must receive
a separate writable Hermes home, secrets directory, Tailscale state/socket,
ports, hostname, and A2A credentials. Concurrent agents must never share one
writable Hermes home.

For multi-node work, the launcher preserves the exact scheduler-provided
`$PBS_NODEFILE` under the persistent per-job run directory and exposes that
copy inside the container. Host modules are resolved before Apptainer starts;
the resulting MPI executable/library environment and required Cray, NVIDIA,
libfabric, `/soft`, and PALS paths are passed into the container. Red Shirt may
then invoke host PALS `mpiexec --hostfile ...` directly. A native and a
production-equivalent containerized two-node MPI hello-world are mandatory
before an application is described as MPI-capable.

## Credentials and authorization

Use separate credentials for separate trust boundaries:

1. **Headscale join key** — short-lived, reusable only for controlled retries, and ephemeral-node scoped.
2. **Wesley → Red Shirt Polaris A2A token** — configured on Polaris as a per-peer inbound token whose authenticated identity is `wesley`.
3. **Red Shirt Polaris → Wesley A2A token** — configured on Wesley as a per-peer inbound token whose authenticated identity is `red-shirt-polaris`.
4. **ALCF inference token store** — refreshable Globus credentials owned by the existing ALCF helper.

Credential requirements:

- stage as mode-`0600` files outside git and OCI/SIF layers;
- never use `qsub -v`, command-line bearer values, logs, Discord, or shell history;
- render runtime `.env` material only inside the protected persistent or job-local directory;
- never print, hash, or otherwise disclose credential contents during verification;
- restrict A2A trusted peers to the authenticated expected identity; and
- reject missing, weak, or malformed credentials before launching Hermes.

Agent discovery may expose the non-secret A2A Agent Card over the tailnet, but task calls require bearer authentication. Negative-control requests without or with the wrong token must return HTTP 401.

## Hermes and inference configuration

The compute entrypoint renders Hermes configuration from the live ALCF catalog and token helper.

Requirements:

- only ALCF Inference Service provider URLs;
- `strip_tool_message_name: true` where required by the strict Sophia gateway;
- real per-model serving context lengths;
- reasoning models separated into providers with adequate output-token budgets;
- A2A platform enabled;
- A2A listener bound to `127.0.0.1:9900`;
- advertised public URL set to the tailnet-reachable Red Shirt Polaris URL;
- A2A trusted peer restricted to `wesley`;
- outbound Wesley peer registered under `a2a_agents` with its bearer credential;
- toolsets required for the mission enabled and irrelevant desktop/media toolsets disabled; and
- no dashboard or API-server peer surface required for communication.

The launch model must be verified LIVE immediately before job submission. The deployment must not silently retain the current Sophia default if that model or cluster is offline. If no configured launch model is live and usable, startup exits with an actionable error rather than booting an agent that cannot answer.

The ALCF access token has two independent lifetime constraints: refreshable short-lived access tokens and a non-automatable 30-day session reauthentication boundary. Token refresh failures must be terminally visible in logs/status and must not leave a resident but nonfunctional agent silently running.

## Startup sequence

The compute entrypoint performs these gates in order:

1. Validate mounts, file ownership/modes, required binaries, and immutable deployment metadata.
2. Create a unique mode-`0700` job-local root.
3. Start and probe the selective CONNECT rewriter.
4. Start userspace `tailscaled` with the ALCF proxy and private CA.
5. Join Headscale using `--auth-key=file:<path>` and wait for `BackendState=Running`.
6. Start Tailscale Serve from tailnet `:9900` to local Hermes A2A `127.0.0.1:9900`.
7. Refresh/resolve the ALCF inference token, query catalog/job status, select or validate a live launch model, and issue a direct inference smoke request.
8. Render Hermes config and secret environment.
9. Seed Red Shirt Polaris's SOUL, documentation index, skills, and managed memory.
10. Start `hermes gateway` and wait for the local A2A Agent Card.
11. Verify the Agent Card through the local userspace Tailscale stack, not by directly curling the host's own tailnet IP.
12. Write a machine-readable READY record containing only non-secret state and provenance.

Any failed mandatory gate stops startup and triggers cleanup.

## Outbound userspace-networking gate

Polaris has no kernel tailnet route. The standard A2A client uses ordinary HTTP, so Red Shirt Polaris's outbound call to Wesley must be exercised through Tailscale's userspace outbound HTTP proxy or SOCKS path.

Acceptance rule:

- first test the standard Hermes A2A client with `HTTP_PROXY`/`NO_PROXY` scoped so tailnet A2A uses Tailscale while ALCF inference continues through the ALCF proxy;
- if Python's A2A HTTP path does not honor the userspace proxy correctly, add a narrow loopback forwarding endpoint for Wesley's exact tailnet authority;
- do not globally route inference or arbitrary traffic through Tailscale; and
- verify the exact standard Hermes A2A call, not merely `curl` or ICMP/disco ping.

## PBS deployment

Provide a reusable PBS launcher without an embedded `#PBS -A` allocation. Submit with an explicit approved project, currently `datascience` for this Polaris deployment.

The launcher:

- requests Polaris, required filesystems, and an appropriate walltime/queue;
- validates SIF checksum before execution;
- binds only destinations that exist in the immutable SIF;
- mounts credentials read-only and persistent Hermes state read-write;
- keeps temporary state on `/local/scratch`;
- records PBS job ID, host, image/SIF provenance, and non-secret readiness state;
- forwards `TERM`/`INT` to the entrypoint; and
- writes terminal status and diagnostics even when startup or runtime fails.

A successful `qsub` or `qdel` return code is not sufficient evidence. Poll `qstat -xf`, inspect `Exit_status`, and verify disappearance after deletion.

## Bidirectional acceptance test

The deployment is complete only after all gates below are measured on the actual Polaris compute job:

1. SIF checksum and executable version probes pass.
2. Headscale registration succeeds and Tailscale reports `BackendState=Running`.
3. DERP connectivity is recorded; failed direct UDP/disco ping is informational if application transport passes.
4. A direct ALCF Inference Service request from the compute job returns expected nonempty model content.
5. Red Shirt Polaris's Agent Card is reachable from Wesley over the tailnet.
6. An unauthenticated Wesley request to Red Shirt Polaris returns HTTP 401.
7. Wesley sends an authenticated standard A2A task to Red Shirt Polaris.
8. Red Shirt Polaris answers using the configured ALCF model; response and A2A audit evidence are read back.
9. Red Shirt Polaris sends an authenticated standard A2A task to Wesley through the userspace network path.
10. Wesley returns a concrete response; Polaris-side result and Wesley-side audit/session evidence are read back.
11. Red Shirt Polaris answers an identity/environment probe consistently with its SOUL: name, compute-node locus, ALCF inference backend, proxy/network constraints, and local documentation index.
12. A documentation-grounding probe causes it to consult and cite an included Polaris document rather than inventing site-specific instructions.

No success claim is made from process existence, an HTTP health check alone, or an agent's self-report.

## Teardown and recovery

On normal exit or signal:

1. stop accepting new A2A work;
2. preserve relevant non-secret Hermes/A2A logs and state;
3. remove Tailscale Serve configuration;
4. issue Tailscale logout;
5. stop Hermes, Tailscale, and the local rewriter with bounded waits;
6. remove job-local state; and
7. emit a machine-readable terminal record.

Afterwards:

- re-poll PBS until the job is absent or terminal;
- read back Headscale state and remove any residual disposable node;
- expire the deployment join key when no longer needed;
- audit the user's PBS queue for held or stray jobs; and
- retain persistent Hermes state and the reproducible SIF so a restart is cheap.

## Testing and review

Implementation follows test-first development for executable behavior. Tests cover:

- secret-file validation and non-disclosure;
- proxy selection and isolation between tailnet A2A and ALCF inference;
- startup ordering and fail-closed behavior;
- signal forwarding and cleanup;
- generated Hermes A2A configuration;
- SOUL managed seeding and preservation of user edits;
- documentation index completeness and source metadata;
- live-model validation/failure;
- machine-readable readiness/terminal records; and
- PBS launcher invariants.

Before merge:

- full local test suite and shell syntax checks;
- independent security/design review of the exact commit;
- multi-arch CI build;
- registry manifest and revision-label readback; and
- no untracked credential or generated-state files in the commit.

## Deliverables

- dedicated compute Dockerfile/stage;
- compute entrypoint and helper scripts;
- Red Shirt Polaris `SOUL.md`;
- curated Polaris docs snapshot plus indexed provenance;
- Hermes compute config template;
- PBS SIF builder and resident-job launcher;
- automated tests;
- operator runbook;
- updated `deploy/polaris/STATUS.md` with immutable artifacts and measured results; and
- raw non-secret acceptance evidence stored on Polaris and summarized in the repository.
