# Red Shirt Polaris deployment notes (measured, not official ALCF policy)

**This document is a locally measured operator note about this deployment's
own runtime behavior on Polaris. It is not official ALCF policy, is not
published by ALCF/Argonne, and must not be cited or repeated as if it were.**
Where it disagrees with anything in `../official/`, the official snapshot
governs; this file only records what was actually observed running Red Shirt
Polaris's transport stack on a real Polaris compute job.

Retrieved/measured: 2026-09-16. Source of the raw evidence:
`deploy/polaris/STATUS.md` in this repository, backed by real PBS job runs
(`7628008` build job, `7628026` application-transport job) on Polaris compute
node `x3205c0s31b0n0`.

## Measured userspace Tailscale / DERP behavior

- Polaris compute nodes have no direct kernel tailnet route and no direct UDP
  path to the Headscale/DERP infrastructure observed during testing.
  `tailscaled` must run with `--tun=userspace-networking`; there is no kernel
  TUN interface available or required.
- A real compute job joined the private Headscale network and reached
  Tailscale `BackendState=Running`.
- The measured relay path was DERP region 999 (`do-relay`) over TCP — not a
  direct peer-to-peer UDP path. `tailscale ping 100.64.0.2` **failed** in this
  environment (no direct UDP send succeeded to the observed peer address),
  while the actual application-level HTTP request over the userspace SOCKS
  proxy succeeded with HTTP 200. Treat DERP-only relay as expected on Polaris
  compute nodes, not as a failure.

## Real-port-not-ping rule

Because `tailscale ping` (ICMP/disco-style reachability) does not reflect
whether the application transport actually works on this network, transport
readiness on Polaris must be judged by a real port/protocol check against the
target service (an actual HTTP request to the expected endpoint and port),
not by ping-style diagnostics. A failed `tailscale ping` alongside a
successful application-level HTTP 200 response is the observed normal case
here — do not fail a readiness gate on ping alone. `tailscale ping` output is
still worth recording as diagnostic context, but it is not the acceptance
signal.

## Compute proxy requirement

Polaris compute nodes have no direct outbound internet route. Every outbound
HTTPS call this deployment makes that is not tailnet traffic (e.g. Headscale
control-plane traffic, ALCF Inference Service calls) must go through the ALCF
HTTP(S) proxy at `proxy.alcf.anl.gov:3128`, matching the official proxy
guidance in `../official/polaris-getting-started.md`. Headscale control
traffic in this deployment specifically goes through a selective local HTTP
CONNECT rewriter in front of that same ALCF proxy so only the Headscale
hostname is redirected — general inference traffic is not captured by that
rewriter and continues to route through the ALCF proxy directly.

## Links to repo operator/status docs

- Raw measured transport evidence, job IDs, and exact pass/fail results:
  `deploy/polaris/STATUS.md`
- Operator runbook for the underlying probe deployment (credential staging,
  submit, monitor, teardown): `deploy/polaris/README.md`
- Probe PBS launcher used to produce the measurements above:
  `deploy/polaris/headscale-preflight.pbs`
- Probe implementation: `scripts/headscale_probe.sh`

## Caveats

- These findings are specific to the Headscale/Tailscale configuration,
  Polaris network path, and job placement observed on 2026-09-16 during the
  transport-proof jobs cited above. Network behavior on ALCF systems can
  change; re-verify before relying on any of the above for a new deployment
  or after any ALCF network/maintenance change.
- Cleanup verification (expiring the probe pre-auth key, deleting the
  ephemeral probe node, confirming both via server-side readback) had **not**
  been independently completed as of the `STATUS.md` snapshot referenced
  above. Do not assume prior probe identities were already cleaned up without
  re-checking `deploy/polaris/STATUS.md` or repeating the readback.
