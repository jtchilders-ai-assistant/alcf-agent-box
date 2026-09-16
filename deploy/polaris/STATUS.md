# Polaris Headscale and Hermes A2A status

Last updated: 2026-09-16

## Executive status

**Transport verdict: GO for the full Hermes A2A phase, with DERP-only connectivity expected.**

A real Polaris compute-node job joined the private Headscale network, reached Tailscale `BackendState=Running`, and fetched Wesley's Hermes health endpoint through the userspace-networking SOCKS proxy with HTTP 200. Tailscale's disco ping failed because Polaris has no direct UDP route; that failure does not invalidate the successful application-layer request through DERP.

Authenticated bidirectional Hermes A2A has **not** yet been run. Server-side expiry of the pre-auth key and removal of the ephemeral probe node have **not** yet been independently verified.

## Reproducible artifacts

- Repository: `jtchilders-ai-assistant/alcf-agent-box`
- Current integration commit: `14311469571d5b6a0941ca21ad0156f3a5ea28c8`
- Corrected probe image: `ghcr.io/jtchilders-ai-assistant/alcf-agent-headscale-probe:sha-e61a1b3`
- OCI index digest: `sha256:157983581bf121b0cedcb2c45922f65e7e4bd6309b2ad69ee5c6389ecf20fc6e`
- Architectures: `linux/amd64`, `linux/arm64`
- Polaris SIF: `/home/parton/polaris-headscale-preflight/alcf-headscale-probe-sha-e61a1b3.sif`
- SIF SHA-256: `a2c110b27689d754a3bd2c12765d6f4668aec1152ed73d13392ce2708343a973`
- Tailscale: `1.88.3`, commit `9961c097b1781891e3c6b96e5e1194355ff06a6d`

## Verified jobs

### Build

PBS job `7628008` completed with exit status 0. It built the corrected SIF, passed `sha256sum -c`, and executed `tailscale version` from the SIF.

### Successful application transport

PBS job `7628026` ran on `x3205c0s31b0n0`.

- Headscale private-CA `/health`: PASS, HTTP 200
- `tailscaled` userspace daemon startup: PASS
- Headscale registration: PASS
- Tailscale backend state: PASS, `Running`
- Home relay: DERP region 999, `do-relay`
- `tailscale ping 100.64.0.2`: FAIL
- Wesley health through userspace SOCKS: PASS, HTTP 200
- Cleanup inside the job: Tailscale logout was attempted and the daemon exited

The ping logs show failed direct UDP sends to `140.221.17.14`; the application request succeeding through SOCKS demonstrates that the DERP data path itself worked.

## Earlier failures and fixes

- `7627909`: Apptainer rejected binds to destination paths absent from the immutable SIF.
  - Fix: bind only to existing image directories and pass mounted file paths explicitly.
- `7627986` and `7627987`: proxy readiness falsely failed because nested Bash/Python quoting removed the string quotes around `127.0.0.1`.
  - Fix: preserve the Python host literal; a dedicated compute diagnostic (`7627988`) returned `connect_ex=0` for both `127.0.0.1` and `localhost`.

## Interpretation

The transport acceptance gate is the application-level request through the userspace SOCKS proxy, not Tailscale's disco ping. On Polaris:

- direct UDP is unavailable;
- Headscale control traffic works through the selective local CONNECT rewriter and ALCF HTTPS proxy;
- DERP over TCP/443 works;
- application traffic to Wesley works through Tailscale userspace networking.

`tailscale ping` remains useful diagnostic evidence and should be recorded, but it must not by itself make the transport probe fail when the exact application endpoint succeeds.

## Immediate cleanup gate

Before deploying the full agent:

1. On the Headscale server, list pre-auth keys and nodes.
2. Expire the probe pre-auth key by numeric ID.
3. Delete any remaining `probe-*` node by numeric identifier.
4. List both resources again and verify the key is expired and the node is absent.
5. Remove `/home/parton/polaris-headscale-preflight/headscale-auth.key` from Polaris and verify absence.

No cleanup claim should be made until server-side readback is captured.

## Next implementation phase

1. Build a compute-specific Hermes OCI image based on the existing Agent-in-a-Box work.
2. Run `tailscaled --tun=userspace-networking` with the verified selective CONNECT rewriter.
3. Run Hermes against `https://inference-api.alcf.anl.gov` through the ALCF proxy.
4. Keep Hermes bound to loopback and expose A2A only on the tailnet using Tailscale Serve or an equivalent userspace-networking forwarder.
5. Mount Headscale, inference, and A2A credentials from mode-0600 files; do not place them in OCI layers, git, `qsub -v`, environment variables, command lines, or PBS output.
6. Verify authenticated communication in both directions:
   - Wesley to Polaris Hermes;
   - Polaris Hermes to Wesley;
   - read back concrete API responses and relevant logs.
7. Confirm teardown: logout, remove temporary Headscale identity, and verify no residual compute-node state.

## Evidence locations

- Operator procedure: `deploy/polaris/README.md`
- PBS launcher: `deploy/polaris/headscale-preflight.pbs`
- SIF builder: `deploy/polaris/build-probe-sif.sh`
- Probe implementation: `scripts/headscale_probe.sh`
- Polaris result directory: `/home/parton/polaris-headscale-preflight/results/7628026.polaris-pbs-01.hsn.cm.polaris.alcf.anl.gov/`
