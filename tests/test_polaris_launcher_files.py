"""Static contract tests for Polaris Headscale probe deployment files."""
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "polaris"
README = DEPLOY / "README.md"
BUILD = DEPLOY / "build-probe-sif.sh"
PBS = DEPLOY / "headscale-preflight.pbs"
IMAGE = "ghcr.io/jtchilders-ai-assistant/alcf-agent-headscale-probe:sha-d26b9fd"
DIGEST = "sha256:e39f7851e4bac35fe508e870e1439ddd08916751268305deff0f2b52e18d7e46"
FINGERPRINT = "71:63:DE:FF:81:7C:E9:18:DA:F5:5F:7D:64:0B:C4:A8:FE:91:C7:D4:EE:25:71:4D:FB:A9:5B:AE:D3:9F:F3:8D"


def text(path: Path) -> str:
    assert path.is_file(), f"missing {path.relative_to(ROOT)}"
    return path.read_text()


def test_files_exist():
    for path in (README, BUILD, PBS):
        assert path.is_file()


def test_shell_syntax():
    for path in (BUILD, PBS):
        p = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        assert p.returncode == 0, p.stderr


def test_immutable_image_and_digest_documented_and_used():
    for path in (README, BUILD, PBS):
        body = text(path)
        assert IMAGE in body
        assert DIGEST in body
    assert ":latest" not in text(BUILD)
    assert ":latest" not in text(PBS)


def test_module_order_and_apptainer_only():
    body = text(BUILD)
    assert body.index("ml use /soft/modulefiles") < body.index("ml spack-pe-base") < body.index("ml apptainer")
    assert "apptainer" in body
    assert not re.search(r"^\s*(docker|podman)\s", body, re.I | re.M)


def test_build_uses_local_scratch_and_bounded_squashfs():
    body = text(BUILD)
    assert "/local/scratch" in body
    assert "APPTAINER_TMPDIR" in body and "APPTAINER_CACHEDIR" in body
    assert "-processors 4 -mem 4G" in body


def test_pbs_resources_and_project_submission():
    body = text(PBS)
    assert "#PBS -q debug" in body
    assert "#PBS -l filesystems=home" in body
    assert re.search(r"#PBS -l walltime=00:(?:0\d|1\d|20):00", body)
    assert not re.search(r"^#PBS\s+-A", body, re.M)
    assert "qsub -A datascience" in text(README)


def test_credentials_are_host_files_mode_600_and_not_env():
    body = text(PBS)
    assert "headscale-auth.key" in body and "caddy-root.crt" in body
    assert "stat -c '%a'" in body and '"$mode" != "600"' in body
    assert "-r" in body and "-f" in body
    assert re.search(r"apptainer.*(?:--bind|-B)", body, re.S)
    assert re.search(r"headscale-auth\.key[^\n]*:ro|AUTH_KEY_FILE[^\n]*:ro", body)
    assert re.search(r"caddy-root\.crt[^\n]*:ro|CA_FILE[^\n]*:ro", body)
    assert not re.search(r"APPTAINERENV_[A-Z_]*(?:AUTH|KEY|TOKEN|SECRET|PASS)[A-Z_]*", body)
    assert not re.search(r"^\s*qsub\s+.*(?:-v\b|--variable-list)", text(README), re.M)


def test_launcher_proxy_and_runtime_contract():
    body = text(PBS)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        assert name in body
    assert "proxy.alcf.anl.gov:3128" in body
    assert "APPTAINERENV_" in body
    assert "--writable" not in body
    assert "--net" not in body and "--network" not in body
    assert "result" in body.lower() and "hostname" in body and "apptainer --version" in body


def test_launcher_cleans_sensitive_node_local_state_on_exit():
    body = text(PBS)
    assert "trap cleanup_state EXIT" in body
    assert 'rm -rf "$SCRATCH_ROOT"' in body


def test_json_validation_preserves_probe_failure_and_fails_closed_on_success():
    body = text(PBS)
    assert "json_rc=$?" in body
    assert '[ "$probe_rc" -ne 0 ]' in body
    assert '[ "$json_rc" -ne 0 ]' in body
    assert "exit \"$probe_rc\"" in body


def test_secret_env_guard_covers_common_secret_names():
    body = text(PBS)
    assert not re.search(
        r"APPTAINERENV_[A-Z_]*(?:AUTH|KEY|TOKEN|SECRET|PASS)[A-Z_]*", body
    )


def test_documented_key_lifecycle_and_ca_verification():
    body = text(README)
    for phrase in ("short-lived", "reusable", "ephemeral", "chmod 600", "expire", "remove"):
        assert phrase.lower() in body.lower()
    assert FINGERPRINT in body
    assert "openssl" in body and "fingerprint" in body.lower()
    assert "placeholder" in body.lower()
    assert "A2A" not in body or "not" in body.lower()


def test_headscale_v0293_cli_syntax_is_documented_correctly():
    body = text(README)
    assert "headscale users list" in body
    assert "--user PLACEHOLDER_USER_ID" in body
    assert "preauthkeys expire --id PLACEHOLDER_KEY_ID" in body
    assert "preauthkeys expire --user" not in body
    assert "nodes delete --identifier PLACEHOLDER_NODE_ID" in body
