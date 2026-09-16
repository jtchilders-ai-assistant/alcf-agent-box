# Polaris Headscale transport preflight

This procedure runs the minimal Tailscale transport probe on a Polaris compute node. It does **not** deploy Hermes and does **not** use an A2A token.

## Pinned artifact

- OCI image: `ghcr.io/jtchilders-ai-assistant/alcf-agent-headscale-probe:sha-d26b9fd`
- OCI manifest digest: `sha256:e39f7851e4bac35fe508e870e1439ddd08916751268305deff0f2b52e18d7e46`
- Expected platforms: `linux/amd64`, `linux/arm64`

Do not replace the immutable SHA tag with `latest` for a recorded preflight.

## Prepare trust material

The Caddy root certificate is public trust material, not a private key. Place it directly on Polaris:

```bash
install -d -m 700 "$HOME/polaris-headscale-preflight"
install -m 600 /secure/source/caddy-root.crt \
  "$HOME/polaris-headscale-preflight/caddy-root.crt"
openssl x509 -in "$HOME/polaris-headscale-preflight/caddy-root.crt" \
  -noout -fingerprint -sha256
```

The expected SHA-256 certificate fingerprint is:

```text
71:63:DE:FF:81:7C:E9:18:DA:F5:5F:7D:64:0B:C4:A8:FE:91:C7:D4:EE:25:71:4D:FB:A9:5B:AE:D3:9F:F3:8D
```

Stop if it differs. Never use `curl -k` or disable certificate verification.

## Create and stage the Headscale pre-auth key

On the Headscale server, identify the numeric user ID, then create a **short-lived, reusable, ephemeral** pre-auth key. Headscale v0.29.3 requires a numeric ID for `--user`. Substitute real values only in the interactive server shell; the values below are placeholders:

```bash
cd /opt/headscale
docker compose exec headscale headscale users list
# PLACEHOLDER_USER_ID must be replaced with the numeric ID from the live list.
docker compose exec headscale headscale preauthkeys create \
  --user PLACEHOLDER_USER_ID \
  --expiration 30m \
  --reusable \
  --ephemeral
```

If policy supports tags, use only a narrowly authorized probe tag. Do not paste the returned key into git, Discord, shell history, `qsub -v`, PBS logs, or an environment variable.

Transfer it directly from a trusted terminal into this Polaris file and immediately restrict its mode:

```bash
install -m 600 /secure/source/headscale-auth.key \
  "$HOME/polaris-headscale-preflight/headscale-auth.key"
chmod 600 "$HOME/polaris-headscale-preflight/headscale-auth.key"
test "$(stat -c '%a' "$HOME/polaris-headscale-preflight/headscale-auth.key")" = 600
```

The launcher verifies that both the key and CA are readable regular files with mode exactly `600` before starting Apptainer.

## Build the SIF

Run this on a suitable Polaris node. The script loads `spack-pe-base` before `apptainer`, uses job-local `/local/scratch`, and limits `mksquashfs` to four processors and 4 GiB:

```bash
bash deploy/polaris/build-probe-sif.sh
```

It writes:

```text
$HOME/polaris-headscale-preflight/alcf-headscale-probe-sha-d26b9fd.sif
$HOME/polaris-headscale-preflight/alcf-headscale-probe-sha-d26b9fd.sif.sha256
```

## Submit the compute probe

The project is supplied explicitly because PBS does not expand shell variables in directives:

```bash
qsub -A datascience deploy/polaris/headscale-preflight.pbs
```

Do not use `qsub -v` for credentials. Results are written under:

```text
$HOME/polaris-headscale-preflight/results/<PBS_JOBID>/
```

Inspect `metadata.txt`, `result.json`, and `probe.stderr`. Success requires valid Headscale TLS, a Running Tailscale backend, a peer ping to `100.64.0.2`, and an HTTP response from `http://100.64.0.2:8642/health` through the userspace SOCKS proxy.

## Revoke and clean up

After the attempt, expire the pre-auth key and remove the temporary probe node from the Headscale server. Use IDs obtained from live listing commands—never copy placeholder IDs literally:

```bash
cd /opt/headscale
docker compose exec headscale headscale preauthkeys list --user PLACEHOLDER_USER_ID
docker compose exec headscale headscale preauthkeys expire --id PLACEHOLDER_KEY_ID
docker compose exec headscale headscale nodes list
docker compose exec headscale headscale nodes delete --identifier PLACEHOLDER_NODE_ID
docker compose exec headscale headscale nodes list
```

Verify the key is expired and the probe node is absent. Then securely remove the staged key from Polaris:

```bash
rm -f "$HOME/polaris-headscale-preflight/headscale-auth.key"
test ! -e "$HOME/polaris-headscale-preflight/headscale-auth.key"
```

Keep the CA and SIF only if further tests are planned. A Hermes A2A credential is deliberately **not** part of this transport-only phase.
