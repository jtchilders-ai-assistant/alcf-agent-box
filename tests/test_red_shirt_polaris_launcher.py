#!/usr/bin/env python3
"""Static contract tests for the Red Shirt Polaris reproducible SIF build
and resident PBS launcher.

Design: docs/superpowers/specs/2026-09-16-red-shirt-polaris-design.md
Plan:   docs/superpowers/plans/2026-09-16-red-shirt-polaris.md (Task 6)

These are static/structural tests only -- there is no real Polaris cluster,
Apptainer runtime, or PBS scheduler available in this environment, so
nothing here submits a job or builds an image. Every assertion inspects the
committed shell source for the invariants the design and plan require.

Run: pytest -q tests/test_red_shirt_polaris_launcher.py
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "polaris"
BUILD = DEPLOY / "build-red-shirt-sif.sh"
PBS = DEPLOY / "red-shirt-polaris.pbs"
README = DEPLOY / "RED_SHIRT_README.md"


def text(path: Path) -> str:
    assert path.is_file(), f"missing {path.relative_to(ROOT)}"
    return path.read_text()


# ---------------------------------------------------------------------------
# Existence + syntax
# ---------------------------------------------------------------------------

def test_files_exist():
    for path in (BUILD, PBS, README):
        assert path.is_file()


def test_shell_syntax():
    for path in (BUILD, PBS):
        p = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        assert p.returncode == 0, p.stderr


# ---------------------------------------------------------------------------
# Account: never in directives, never via qsub -v, selected only at qsub time
# ---------------------------------------------------------------------------

def test_pbs_keeps_allocation_out_of_directives():
    body = text(PBS)
    assert "#PBS -A" not in body
    assert not re.search(r"^\s*#PBS\s+-A\b", body, re.M)
    assert "qsub -v" not in body
    assert "sha256sum -c" in body
    assert "/local/scratch" in body


def test_readme_documents_qsub_with_explicit_account_only_at_submit_time():
    body = text(README)
    assert "qsub -A datascience" in body
    assert not re.search(r"^\s*qsub\s+.*(?:-v\b|--variable-list)", body, re.M)


def test_pbs_never_uses_qsub_v_for_credentials():
    body = text(PBS)
    assert "qsub -v" not in body
    assert "-v " not in body.replace("#PBS -l", "")  # cheap guard; no stray -v flag usage


# ---------------------------------------------------------------------------
# Immutable image pin: fail closed until CI publish + digest are recorded
# ---------------------------------------------------------------------------

def test_build_script_fails_closed_without_a_published_image_pin():
    body = text(BUILD)
    # No default/hardcoded ghcr tag or digest: this task cannot know the real
    # values because the image has not been published from this exact commit
    # yet. The script must refuse to silently proceed -- bash's ${VAR:?msg}
    # aborts immediately with an actionable message when the operator has
    # not supplied the CI-published, digest-pinned reference.
    assert re.search(r"RED_SHIRT_IMAGE:\?\S", body), \
        "RED_SHIRT_IMAGE must use ${RED_SHIRT_IMAGE:?...} to fail closed"
    assert re.search(r"RED_SHIRT_DIGEST:\?\S", body), \
        "RED_SHIRT_DIGEST must use ${RED_SHIRT_DIGEST:?...} to fail closed"


def test_build_script_validates_digest_shape_and_pins_by_digest():
    body = text(BUILD)
    assert "sha256:" in body
    # The image actually built from must be the tag@digest form, not the
    # tag alone -- otherwise a compromised/updated tag could silently swap
    # the bits actually fetched.
    assert re.search(r'"\$\{?RED_SHIRT_IMAGE\}?@\$\{?RED_SHIRT_DIGEST\}?"', body) or \
        "PINNED_IMAGE" in body


def test_build_script_never_defaults_to_a_mutable_tag():
    body = text(BUILD)
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert not re.search(r"RED_SHIRT_IMAGE:-", line), \
            f"RED_SHIRT_IMAGE must never have a silent default: {line!r}"
        assert not re.search(r"RED_SHIRT_DIGEST:-", line), \
            f"RED_SHIRT_DIGEST must never have a silent default: {line!r}"


# ---------------------------------------------------------------------------
# Module order + Apptainer-only + bounded SquashFS + scratch placement
# ---------------------------------------------------------------------------

def test_module_order_and_apptainer_only():
    body = text(BUILD)
    assert body.index("ml use /soft/modulefiles") < body.index("ml spack-pe-base") < body.index("ml apptainer")
    assert not re.search(r"^\s*(docker|podman)\s", body, re.I | re.M)


def test_pbs_also_loads_modules_in_verified_order():
    body = text(PBS)
    assert body.index("ml use /soft/modulefiles") < body.index("ml spack-pe-base") < body.index("ml apptainer")


def test_build_uses_local_scratch_and_bounded_squashfs():
    body = text(BUILD)
    assert "/local/scratch" in body
    assert "APPTAINER_TMPDIR" in body and "APPTAINER_CACHEDIR" in body
    assert re.search(r"-processors\s+\d+\s+-mem\s+\d+G", body), \
        "mksquashfs resources must be explicitly bounded (processors + memory)"


def test_build_scratch_dirs_are_job_scoped_and_removed():
    body = text(BUILD)
    assert "SCRATCH_ROOT" in body
    assert re.search(r"rm -rf .*\$SCRATCH_ROOT", body), \
        "build scratch must be cleaned up, not left behind on shared storage"


# ---------------------------------------------------------------------------
# SIF checksum generation (build) and readback (PBS) + in-SIF version checks
# ---------------------------------------------------------------------------

def test_build_generates_sif_checksum():
    body = text(BUILD)
    assert re.search(r"sha256sum\s+\"\$SIF\"", body)
    assert ".sha256" in body


def test_build_verifies_hermes_and_tailscale_inside_the_sif():
    body = text(BUILD)
    assert re.search(r'apptainer exec\s+"\$SIF"\s+hermes\s+--version', body)
    assert re.search(r'apptainer exec\s+"\$SIF"\s+tailscale\s+version', body)


def test_pbs_reads_back_sif_checksum_before_running():
    body = text(PBS)
    assert "sha256sum -c" in body
    idx_check = body.index("sha256sum -c")
    idx_apptainer = body.index("apptainer exec")
    assert idx_check < idx_apptainer, "checksum verification must precede execution"


# ---------------------------------------------------------------------------
# Credential handling: metadata-only validation, mode 0600, never printed
# ---------------------------------------------------------------------------

def test_pbs_validates_credential_metadata_only():
    body = text(PBS)
    assert "stat -c '%a'" in body
    assert '"$mode" != "600"' in body
    # Metadata checks only -- never cat/read the credential's own content.
    for cred_var in ("HEADSCALE_KEY_FILE", "INBOUND_A2A_FILE", "OUTBOUND_A2A_FILE"):
        assert not re.search(rf"cat\s+\"\$\{{?{cred_var}\}}?\"", body)


def test_pbs_never_places_secrets_in_argv_env_or_logs():
    body = text(PBS)
    # No APPTAINERENV_* variable carries a secret-shaped name with a literal
    # value assignment (only *_FILE path references are permitted).
    assert not re.search(
        r"APPTAINERENV_[A-Z_]*(?:AUTH|KEY|TOKEN|SECRET|PASS)[A-Z_]*=(?!\"/)",
        body,
    )
    # Credentials are passed as mounted file paths, never as --auth-key=<value>
    # or bearer-token-shaped CLI literals.
    assert not re.search(r"--auth-key=(?!file:)\S", body)
    assert "Authorization:" not in body


def test_credential_files_required_mode_0600():
    body = text(PBS)
    assert body.count('"$mode" != "600"') >= 1
    for label in ("headscale-auth.key", "inbound-a2a.token", "outbound-a2a.token", "caddy-root.crt"):
        assert label in body


# ---------------------------------------------------------------------------
# Binds: persistent HERMES_HOME, read-only secrets, job-local runtime state
# ---------------------------------------------------------------------------

def test_pbs_binds_persistent_hermes_home():
    body = text(PBS)
    assert re.search(r'--bind\s+"\$HERMES_HOME_DIR:/opt/data"', body)


def test_pbs_binds_secrets_read_only_to_an_existing_image_destination():
    body = text(PBS)
    assert re.search(r'--bind\s+"\$SECRETS_DIR:/mnt/secrets:ro"', body)
    assert "--writable" not in body
    assert "--writable-tmpfs" not in body


def test_pbs_uses_job_local_scratch_for_runtime_state():
    body = text(PBS)
    assert "/local/scratch" in body
    assert re.search(r'--bind\s+"\$STATE_DIR:/tmp"', body)


def test_pbs_binds_only_to_destinations_guaranteed_present_in_image():
    """Every --bind destination must be one the Dockerfile actually creates
    (or a universally-present path like /tmp or /mnt) -- Apptainer refuses
    to bind onto a path absent from a read-only SIF."""
    body = text(PBS)
    destinations = re.findall(r'--bind\s+"[^:"]+:([^":]+?)(?::ro)?"', body)
    assert destinations, "expected at least one --bind directive"
    allowed = {"/opt/data", "/mnt/secrets", "/tmp"}
    for dest in destinations:
        assert dest in allowed, f"unexpected bind destination not verified present in image: {dest}"


# ---------------------------------------------------------------------------
# No dashboard / public listener
# ---------------------------------------------------------------------------

def test_pbs_never_exposes_a_dashboard_or_public_listener():
    body = text(PBS)
    assert "--net" not in body and "--network" not in body
    assert "dashboard" not in body.lower()
    assert "0.0.0.0" not in body


# ---------------------------------------------------------------------------
# Signal-safe cleanup + persistent readiness/terminal evidence
# ---------------------------------------------------------------------------

def test_pbs_traps_signals_and_forwards_to_the_apptainer_process():
    body = text(PBS)
    assert re.search(r"trap\s+cleanup\s+EXIT\s+INT\s+TERM", body)
    assert "kill -TERM \"$APPTAINER_PID\"" in body
    assert 'wait "$APPTAINER_PID"' in body


def test_pbs_cleans_up_job_local_scratch_on_exit():
    body = text(PBS)
    assert re.search(r"rm -rf .*\$SCRATCH_ROOT", body)


def test_pbs_writes_persistent_terminal_evidence():
    body = text(PBS)
    assert "runs/" in body or "RUN_DIR" in body
    assert "terminal" in body.lower()
    # The terminal record must be written under the persistent BASE_DIR tree,
    # not only under ephemeral job-local scratch.
    assert re.search(r"HERMES_HOME_DIR\S*runs|BASE_DIR\S*runs", body)


def test_pbs_records_readiness_path_for_the_in_container_probe():
    body = text(PBS)
    assert "READY_RECORD" in body or "APPTAINERENV_READY_RECORD" in body


# ---------------------------------------------------------------------------
# Runbook: stage / submit / monitor / failure / teardown + qdel re-poll
# ---------------------------------------------------------------------------

def test_readme_covers_full_operator_lifecycle():
    body = text(README).lower()
    for phrase in ("stage", "submit", "monitor", "failure", "teardown"):
        assert phrase in body, f"runbook is missing a '{phrase}' section"


def test_readme_requires_repolling_qstat_after_qdel():
    body = text(README)
    assert "qdel" in body
    assert "qstat" in body
    idx_qdel = body.index("qdel")
    # Some mention of qstat must appear after qdel is introduced, documenting
    # the re-poll-after-delete verification step (qdel's own exit code is not
    # sufficient evidence per the design spec).
    assert "qstat" in body[idx_qdel:], \
        "README must re-poll qstat after qdel, not just trust qdel's exit code"


def test_readme_never_recommends_qsub_v_for_secrets():
    body = text(README)
    assert not re.search(r"qsub\s+-v\s+\S*(?:token|key|secret|auth)", body, re.I)
