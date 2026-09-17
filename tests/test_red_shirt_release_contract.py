from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PBS = ROOT / "deploy" / "polaris" / "red-shirt-polaris.pbs"
DOCKERFILE = ROOT / "Dockerfile.red-shirt-polaris"


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_pbs_exports_the_entrypoint_runtime_contract():
    body = text(PBS)
    required_assignments = {
        'APPTAINERENV_HERMES_HOME': '/opt/data',
        'APPTAINERENV_RED_SHIRT_HOME': '/opt/data',
        'APPTAINERENV_RED_SHIRT_JOB_PARENT': '/tmp',
        'APPTAINERENV_RED_SHIRT_JOB_ROOT': '/tmp/red-shirt-runtime',
        'APPTAINERENV_RED_SHIRT_HEADSCALE_KEY_FILE': '/mnt/secrets/headscale-auth.key',
        'APPTAINERENV_RED_SHIRT_INBOUND_A2A_FILE': '/mnt/secrets/inbound-a2a.token',
        'APPTAINERENV_RED_SHIRT_OUTBOUND_A2A_FILE': '/mnt/secrets/outbound-a2a.token',
        'APPTAINERENV_RED_SHIRT_TOKEN_HELPER': '/opt/red-shirt-polaris/alcf_combined_auth.py',
        'APPTAINERENV_RED_SHIRT_HEADSCALE_URL': 'https://143.198.112.69.sslip.io',
        'APPTAINERENV_RED_SHIRT_PREFERRED_MODEL': 'openai/gpt-oss-120b',
        'APPTAINERENV_RED_SHIRT_WESLEY_URL': 'http://100.64.0.2:9900/',
        'APPTAINERENV_RED_SHIRT_READY_OUTPUT': f'/opt/data/runs/${{JOB_ID}}/ready.json',
        'APPTAINERENV_RED_SHIRT_TERMINAL_OUTPUT': f'/opt/data/runs/${{JOB_ID}}/terminal.json',
    }
    for name, value in required_assignments.items():
        expected = f'export {name}="{value}"'
        assert expected in body, f"missing launcher→entrypoint contract: {expected}"


def test_pbs_does_not_export_obsolete_unconsumed_runtime_names():
    body = text(PBS)
    obsolete = (
        'APPTAINERENV_HEADSCALE_KEY_FILE=',
        'APPTAINERENV_INBOUND_A2A_FILE=',
        'APPTAINERENV_OUTBOUND_A2A_FILE=',
        'APPTAINERENV_TS_STATE_DIR=',
        'APPTAINERENV_READY_RECORD=',
        'APPTAINERENV_TERMINAL_RECORD=',
    )
    for token in obsolete:
        assert token not in body


def test_compute_image_contains_runtime_token_helper():
    body = text(DOCKERFILE)
    assert (
        'COPY scripts/alcf_combined_auth.py '
        '/opt/red-shirt-polaris/alcf_combined_auth.py'
    ) in body


def test_entrypoint_routes_inference_smoke_through_alcf_proxy():
    body = text(ROOT / 'scripts' / 'red_shirt_entrypoint.sh')
    smoke = body[body.index('"$PROBE_PY" inference'):body.index('log "inference smoke OK"')]
    assert '--proxy "$ALCF_PROXY_URL"' in smoke


def test_entrypoint_requires_and_renders_real_a2a_public_url():
    body = text(ROOT / 'scripts' / 'red_shirt_entrypoint.sh')
    assert 'A2A_PUBLIC_URL="http://$TAILNET_IP:$A2A_PORT/"' in body
    render_start = body.index('if ! "$PYTHON_BIN" "$CONFIG_PY" render')
    render_end = body.index('SELECTED_MODEL="$(awk', render_start)
    render = body[render_start:render_end]
    assert '--a2a-public-url "$A2A_PUBLIC_URL"' in render
