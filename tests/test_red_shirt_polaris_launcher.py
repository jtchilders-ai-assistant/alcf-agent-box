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

import json
import re
import signal
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "polaris"
BUILD = DEPLOY / "build-red-shirt-sif.sh"
PBS = DEPLOY / "red-shirt-polaris.pbs"
CAMPAIGN_PBS = DEPLOY / "red-shirt-pepper-campaign.pbs"
README = DEPLOY / "RED_SHIRT_README.md"
DOCKERFILE = ROOT / "Dockerfile.red-shirt-polaris"


def text(path: Path) -> str:
    assert path.is_file(), f"missing {path.relative_to(ROOT)}"
    return path.read_text()


# ---------------------------------------------------------------------------
# Existence + syntax
# ---------------------------------------------------------------------------

def test_campaign_launcher_wires_reviewed_attempt_contract():
    body = text(CAMPAIGN_PBS)
    required = (
        "red_shirt_task_context.py",
        "red_shirt_campaign.py",
        "red_shirt_host_watcher.sh",
        "red_shirt_toolchain_manifest.py",
        "red_shirt_toolchain_preflight.py",
        "pepper-gpu-8rank.md",
        "--hermes-timeout",
        "RED_SHIRT_ENV_PROFILE_ID",
        "RED_SHIRT_EXPECTED_RANKS=8",
        "RED_SHIRT_EXPECTED_HOSTS=2",
    )
    for fragment in required:
        assert fragment in body
    assert "#PBS -l select=2:system=polaris" in body
    assert "#PBS -A" not in body
    assert "qsub -v" not in body
    assert "flock -n" in body
    assert "attempt-ledger" in body
    assert "MAX_ATTEMPTS" in body
    assert "sha256sum -c" in body
    assert "trap" in body and "TERM" in body
    assert 'python3 "$TOOLS/red_shirt_toolchain_preflight.py"' not in body
    assert 'RED_SHIRT_KOKKOS_PREFIX:?' not in body
    assert 'RED_SHIRT_PEPPER_SOURCE:?' not in body
    assert 'RED_SHIRT_PEPPER_CACHE_INIT:?' not in body
    assert 'command -v nvcc' not in body
    assert 'command -v CC' not in body
    assert 'if [ -s "$RED_SHIRT_OUTPUT_DIR/run.ini" ]' in body
    watcher_start = body.index('"$HOST_WATCHER" >')
    for helper in (
        "red_shirt_host_bridge.sh",
        "red_shirt_host_watcher.sh",
        "red_shirt_rank_wrapper.sh",
        "red_shirt_mpi_env.sh",
        "red_shirt_toolchain_stage.sh",
        "red_shirt_toolchain_manifest.py",
        "red_shirt_toolchain_preflight.py",
    ):
        assert body.index(helper) < watcher_start
    assert body.index('source "$MPI_ENV_HELPER"') < body.index('PROFILE_INPUT="$RUNTIME/environment-profile.txt"')
    assert "ml load cray-mpich\n" not in body


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
    for line in body.splitlines():
        if re.search(r"^\s*qsub\s+.*(?:-v\b|--variable-list)", line):
            assert not re.search(r"(?:TOKEN|SECRET|AUTH|PASSWORD|KEY)=", line, re.I)


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
    assert "SCRATCH_BASE" in body
    assert 'if mkdir -p "$LOCAL_SCRATCH"' in body
    assert 'SCRATCH_BASE="$OUT_DIR/.build-scratch"' in body
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
    allowed = {
        "/opt/data",
        "/mnt/secrets",
        "/tmp",
        "/opt/cray",
        "/opt/nvidia",
        "/opt/cray/libfabric",
        "/soft",
        "$PALS_RUNTIME_DIR",
    }
    for dest in destinations:
        assert dest in allowed, f"unexpected bind destination not verified present in image: {dest}"


def test_dockerfile_creates_the_secrets_bind_mountpoint():
    """The PBS launcher binds the read-only secret/CA directory to
    /mnt/secrets (see test_pbs_binds_secrets_read_only_to_an_existing_image_destination
    above), but Apptainer refuses to bind onto a destination that does not
    already exist inside the read-only SIF. The Dockerfile must therefore
    actually create /mnt/secrets as a dedicated, non-root-readable directory
    -- not merely have the launcher/tests assume or allowlist it -- and do
    so while still root, before the final USER hermes switch."""
    lines = text(DOCKERFILE).splitlines()
    body = "\n".join(lines)

    mkdir_indices = [
        i for i, line in enumerate(lines)
        if re.search(r"\bmkdir\s+(-p\s+)?/mnt/secrets\b", line)
    ]
    assert mkdir_indices, (
        "Dockerfile must create the /mnt/secrets bind mountpoint "
        "(e.g. `RUN mkdir -p /mnt/secrets`) -- the launcher's "
        "--bind \"$SECRETS_DIR:/mnt/secrets:ro\" will fail at runtime "
        "against a read-only SIF that never created this path"
    )

    user_indices = [i for i, line in enumerate(lines) if line.startswith("USER ")]
    assert user_indices, "no USER directive found"
    assert all(i < user_indices[-1] for i in mkdir_indices), (
        "the /mnt/secrets mountpoint must be created while still root, "
        "before the final USER hermes switch"
    )

    # Dedicated, standalone destination -- must not be created as a
    # subdirectory of /opt/red-shirt-polaris (application content) or
    # /opt/data (the persistent HERMES_HOME bind target), and the
    # directory itself must be non-root-readable (traversable) so the
    # unprivileged hermes user can read files bind-mounted under it.
    assert "/opt/red-shirt-polaris/mnt" not in body
    assert "/opt/data/mnt" not in body
    mode_indices = [
        i for i, line in enumerate(lines)
        if re.search(r"\bchmod\s+0?755\s+/mnt/secrets\b", line)
        or re.search(r"\bchmod\s+a\+rx\s+/mnt/secrets\b", line)
    ]
    assert mode_indices, (
        "/mnt/secrets must be explicitly made non-root-readable/traversable "
        "(e.g. `chmod 0755 /mnt/secrets`) so the unprivileged hermes user "
        "can read the read-only bind-mounted credential files under it"
    )


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
    # EXIT, INT, and TERM must each be trapped -- via dedicated handlers,
    # not necessarily the single combined `trap cleanup EXIT INT TERM` form,
    # since a single shared handler cannot distinguish a deferred signal
    # from an ordinary command failure (see the SIGTERM regression test
    # below for why that distinction matters).
    assert re.search(r"trap\s+\S+\s+EXIT\b", body)
    assert re.search(r"trap\s+\S+\s+INT\b", body) or re.search(r"trap\s+'[^']*'\s+INT\b", body)
    assert re.search(r"trap\s+\S+\s+TERM\b", body) or re.search(r"trap\s+'[^']*'\s+TERM\b", body)
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


def test_pbs_writes_persistent_terminal_evidence_on_startup_failure(tmp_path, monkeypatch):
    """Behavioral regression test: a launcher failure that happens before
    Apptainer ever runs (missing RED_SHIRT_IMAGE here) must still leave a
    persistent terminal.json under the run directory, because the cleanup
    trap must be installed before any fallible startup gate.

    The image-pin gate uses an explicit `if [ -z ... ]; then ...; exit 1; fi`
    rather than bash's `${VAR:?msg}` parameter expansion specifically so this
    holds on every bash the launcher might run under, including macOS's
    frozen bash 3.2: under `set -e` + an EXIT trap, `${VAR:?msg}` on an unset
    var aborts the script but a bare `rc=$?` read inside the trap then
    observes `0` instead of the real failure code on bash 3.2 (verified
    directly against `/bin/bash` on this host), which would have corrupted
    terminal.json's exit_code/ok fields. Explicit `exit 1` fixes `$?` before
    the trap fires, so this check is version-independent.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    job_id = "test.0"

    env = {
        "HOME": str(fake_home),
        "USER": "redshirt-test",
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "PBS_JOBID": job_id,
        # Deliberately omit RED_SHIRT_IMAGE / RED_SHIRT_DIGEST so the
        # launcher fails closed at the image-pin gate, well before any
        # module load, credential check, or Apptainer invocation could
        # succeed in this sandbox.
    }
    # `ml` is not available outside a real Polaris login/compute node --
    # provide a no-op shim so the launcher gets past module loading and
    # actually reaches (and fails at) the image-pin gate, exercising the
    # trap's coverage of that gate specifically.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ml_shim = bin_dir / "ml"
    ml_shim.write_text("#!/bin/sh\nexit 0\n")
    ml_shim.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"

    proc = subprocess.run(
        ["bash", str(PBS)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode != 0, "expected the launcher to fail closed without RED_SHIRT_IMAGE"

    terminal_record = fake_home / "red-shirt-polaris" / "runs" / job_id / "terminal.json"
    assert terminal_record.is_file(), (
        "launcher must persist terminal.json even when it fails before "
        f"Apptainer ever runs; stderr was:\n{proc.stderr}"
    )
    payload = json.loads(terminal_record.read_text())
    assert payload["ok"] is False
    assert payload["exit_code"] != 0
    assert payload["pbs_job_id"] == job_id


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


# ---------------------------------------------------------------------------
# SIGTERM regression: `trap cleanup EXIT INT TERM` defers signal delivery
# until the current foreground command returns. If that command happens to
# finish on its own (e.g. a slow `ml` module load) before the signal is
# actually delivered, bash re-enters the trap with `$?` reflecting the
# foreground command's own (successful) exit status -- NOT the signal --
# so cleanup wrongly records exit_code: 0 / ok: true and the script exits
# 0 despite having been sent SIGTERM. This must be fixed with dedicated
# INT/TERM handlers that force a nonzero, signal-derived status and guard
# against the EXIT trap re-running the same cleanup a second time.
# ---------------------------------------------------------------------------

def test_pbs_preserves_signal_failure_status_on_sigterm(tmp_path):
    """Behavioral regression test for the reviewer-reported signal bug.

    A controllable foreground `ml` shim sleeps for a few seconds (standing
    in for a slow module load) before returning 0. SIGTERM is sent to the
    launcher process partway through that sleep. Because the foreground
    child is not itself a signal target here (only the launcher's own pid
    is signalled), bash defers trap delivery until the foreground `ml`
    command returns successfully. On the buggy launcher this makes cleanup
    observe rc=0 from the completed `ml` call and persist a false-positive
    terminal.json (ok: true, exit_code: 0) with process exit status 0. The
    fixed launcher must instead report the process as killed/nonzero and
    persist terminal.json with ok: false and a nonzero exit_code -- and it
    must do so exactly once (no double cleanup through both a signal
    handler and the EXIT trap).
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    job_id = "sigterm-test.0"

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # `ml` stands in for the real module command: it sleeps long enough for
    # the test to deliver SIGTERM mid-sleep, then exits 0 on its own -- this
    # is the exact deferred-signal scenario the reviewer reproduced.
    ml_shim = bin_dir / "ml"
    ml_shim.write_text("#!/bin/sh\nsleep 3\nexit 0\n")
    ml_shim.chmod(0o755)

    env = {
        "HOME": str(fake_home),
        "USER": "redshirt-test",
        "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
        "PBS_JOBID": job_id,
        # Deliberately omit RED_SHIRT_IMAGE / RED_SHIRT_DIGEST: this proves
        # the signal is what terminates the script, not a downstream gate
        # the script would have hit anyway once `ml` returns.
    }

    proc = subprocess.Popen(
        ["bash", str(PBS)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # Give the launcher time to create RUN_DIR, install the trap, and enter
    # the foreground `ml` calls before signalling it.
    time.sleep(1.0)
    proc.send_signal(signal.SIGTERM)

    try:
        stdout, stderr = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        raise AssertionError(
            "launcher did not exit within 15s of SIGTERM; "
            f"stdout={stdout!r} stderr={stderr!r}"
        )

    assert proc.returncode != 0, (
        "launcher must exit nonzero on SIGTERM, not persist the completed "
        f"foreground command's own success status; stdout={stdout!r} "
        f"stderr={stderr!r}"
    )

    terminal_record = fake_home / "red-shirt-polaris" / "runs" / job_id / "terminal.json"
    assert terminal_record.is_file(), (
        "launcher must persist terminal.json on SIGTERM; "
        f"stdout={stdout!r} stderr={stderr!r}"
    )
    payload = json.loads(terminal_record.read_text())
    assert payload["ok"] is False, (
        f"terminal.json must record ok: false for a SIGTERM exit, got {payload}"
    )
    assert payload["exit_code"] != 0, (
        f"terminal.json must record a nonzero exit_code for a SIGTERM exit, got {payload}"
    )
    assert payload["pbs_job_id"] == job_id


def test_campaign_verifies_catalog_through_a_bound_path():
    """The pre-copy schema verification must expose the host catalog inside SIF."""
    body = text(CAMPAIGN_PBS)
    assert re.search(
        r'apptainer.*exec.*--bind\s+"?\$RED_SHIRT_ENV_CATALOG:/environment/site\.sqlite:ro"?',
        body,
        re.DOTALL,
    ), "catalog CLI cannot verify an unbound host path from inside the SIF"
    assert re.search(
        r'red_shirt_env_catalog\.py\s+verify\s+\\?\s*\n\s*--db\s+"?/environment/site\.sqlite"?',
        body,
    ), "campaign must use the catalog CLI's real --db interface"


def test_campaign_reverify_does_not_reuse_wrong_basename_sidecar():
    """A copied sidecar naming its source basename cannot verify site.sqlite."""
    body = text(CAMPAIGN_PBS)
    assert 'sha256sum -c "site.sqlite.sha256"' not in body
    assert re.search(
        r'red_shirt_env_catalog\.py\s+verify\s+\\?\s*\n\s*--db\s+"?/environment/site\.sqlite"?',
        body,
    )


def test_campaign_facts_use_container_paths_and_real_catalog_status():
    body = text(CAMPAIGN_PBS)
    assert '"site_db":"/environment/site.sqlite"' in body.replace(" ", "")
    assert '"overlay":overlay' in body.replace(" ", "")
    assert '"collection_status":"see site db"' not in body
    assert '"snapshot_id"' in body


def test_main_agent_exec_binds_catalog_sidecar_read_only():
    body = text(CAMPAIGN_PBS)
    assert re.search(
        r'--bind\s+"\$ENV_DIR/site\.sqlite\.sha256:/environment/site\.sqlite\.sha256:ro"',
        body,
    ), "observe requires the verified site sidecar inside the running container"


# ---------------------------------------------------------------------------
# Task 4: Environment-catalog image and campaign integration
# ---------------------------------------------------------------------------

def test_dockerfile_ships_env_catalog_cli():
    """The image must copy red_shirt_env_catalog.py and mark it executable."""
    body = text(DOCKERFILE)
    # COPY into /opt/red-shirt-polaris/
    assert re.search(
        r"COPY\s+scripts/red_shirt_env_catalog\.py\s+/opt/red-shirt-polaris/red_shirt_env_catalog\.py",
        body,
    ), "Dockerfile must COPY scripts/red_shirt_env_catalog.py into /opt/red-shirt-polaris/"
    # chmod 0555 must include the catalog CLI
    chmod_block = re.search(r"RUN chmod 0555(.*?)(?:\n\n|\n[A-Z#])", body, re.DOTALL)
    assert chmod_block and "red_shirt_env_catalog.py" in chmod_block.group(0), (
        "Dockerfile must chmod 0555 red_shirt_env_catalog.py in the same RUN chmod block"
    )


def test_dockerfile_compiles_env_catalog_with_pinned_interpreter():
    """The catalog CLI must be byte-compiled during image build using the Hermes venv interpreter."""
    body = text(DOCKERFILE)
    # Must compile with the pinned Hermes interpreter
    assert re.search(
        r"/opt/hermes/\.venv/bin/python\s+-m\s+py_compile\s+/opt/red-shirt-polaris/red_shirt_env_catalog\.py",
        body,
    ), (
        "Dockerfile must compile red_shirt_env_catalog.py with "
        "/opt/hermes/.venv/bin/python -m py_compile"
    )


def test_campaign_requires_env_catalog_and_sidecar():
    """Campaign must fail closed if RED_SHIRT_ENV_CATALOG or its .sha256 are absent."""
    body = text(CAMPAIGN_PBS)
    assert "RED_SHIRT_ENV_CATALOG" in body, "campaign must reference RED_SHIRT_ENV_CATALOG"
    # Must require the variable to be set (bash :? expansion or explicit check)
    assert re.search(r"RED_SHIRT_ENV_CATALOG[}:]", body), (
        "campaign must require RED_SHIRT_ENV_CATALOG to be set"
    )
    # Must verify the .sha256 sidecar exists
    assert re.search(r'"\$\{?RED_SHIRT_ENV_CATALOG\}?"\.sha256|RED_SHIRT_ENV_CATALOG.*\.sha256', body), (
        "campaign must verify the .sha256 sidecar for the catalog"
    )


def test_campaign_verifies_catalog_before_using_it():
    """Campaign must run 'verify' on the catalog before copying or binding it."""
    body = text(CAMPAIGN_PBS)
    # verify sub-command or equivalent sha256sum check
    assert "red_shirt_env_catalog.py" in body and (
        "verify" in body or "sha256sum -c" in body
    ), "campaign must invoke the catalog verify command before using it"
    # verify must precede the copy step
    if "verify" in body:
        idx_verify = body.index("verify")
        # Some copy into $ATTEMPT must follow
        assert re.search(r"\$ATTEMPT/environment", body[idx_verify:]), (
            "catalog verify must precede the copy into $ATTEMPT/environment"
        )


def test_campaign_copies_catalog_into_attempt_mode_0444():
    """Campaign must copy the catalog DB and sidecar into $ATTEMPT/environment/ at mode 0444."""
    body = text(CAMPAIGN_PBS)
    assert re.search(r"\$ATTEMPT/environment", body), (
        "campaign must copy catalog into $ATTEMPT/environment/"
    )
    # install -m 0444 or chmod 0444 after copy
    assert re.search(r"(?:install\s+-m\s+0444|chmod\s+0444)", body), (
        "campaign must set the catalog copy to mode 0444"
    )


def test_campaign_reverifies_catalog_copy():
    """Campaign must re-verify the catalog after copying to detect copy errors."""
    body = text(CAMPAIGN_PBS)
    # Must have two separate checksum/verify invocations
    verify_count = body.count("sha256sum -c")
    verify_cmd_count = len(re.findall(r"red_shirt_env_catalog.*verify", body))
    total_verify = verify_count + verify_cmd_count
    assert total_verify >= 2, (
        "campaign must re-verify the catalog copy (at least two checksum checks): "
        f"found {total_verify}"
    )


def test_campaign_binds_attempt_catalog_read_only():
    """Campaign must bind the attempt catalog copy as read-only inside the SIF."""
    body = text(CAMPAIGN_PBS)
    assert re.search(r"/environment/site\.sqlite.*:ro|:ro.*site\.sqlite", body), (
        "campaign must bind the catalog at /environment/site.sqlite:ro inside the SIF"
    )


def test_campaign_provides_writable_overlay_path():
    """Campaign must create and expose a writable overlay path through the attempt bind."""
    body = text(CAMPAIGN_PBS)
    assert re.search(r"environment.*overlay|overlay.*environment", body, re.IGNORECASE), (
        "campaign must set up a writable overlay path under the attempt"
    )


def test_campaign_exports_catalog_paths_and_facts():
    """Campaign must export catalog paths and add environment_catalog entry to facts.json."""
    body = text(CAMPAIGN_PBS)
    # The facts.json inline Python or heredoc must include environment_catalog
    assert "environment_catalog" in body, (
        "campaign must include 'environment_catalog' in facts.json"
    )
    # Must include the explicit epistemic marker
    assert "discovery_only_not_compatibility_proof" in body, (
        "campaign must include 'discovery_only_not_compatibility_proof' in facts.json"
    )
    # Must record site_checksum or collection_status
    assert re.search(r"site_checksum|collection_status|site_sha256", body), (
        "campaign must record site_checksum or collection_status in environment_catalog facts"
    )


def test_campaign_facts_no_secrets_from_catalog():
    """Catalog-related facts must not carry credentials or raw environment dumps."""
    body = text(CAMPAIGN_PBS)
    # Specifically no APPTAINERENV_ assignments containing catalog-derived token/secret shapes
    catalog_env_section = re.search(
        r"environment_catalog.*?(?=\n\n|\nPY\b|EOF)",
        body,
        re.DOTALL,
    )
    if catalog_env_section:
        section = catalog_env_section.group(0)
        assert not re.search(r"(?:token|password|secret|private_key|bearer)", section, re.I), (
            "environment_catalog facts must not contain credential-shaped values"
        )


def test_dockerfile_bind_destinations_include_environment():
    """If the campaign binds /environment, the Dockerfile must create that mountpoint."""
    # The campaign binds site.sqlite at /environment/site.sqlite.
    # Apptainer refuses to bind onto a path absent from the read-only SIF.
    # The Dockerfile must therefore create /environment as a mountpoint.
    body_campaign = text(CAMPAIGN_PBS)
    if "/environment/site.sqlite" in body_campaign:
        body_df = text(DOCKERFILE)
        assert re.search(r"mkdir\s+(-p\s+)?/environment\b", body_df), (
            "Dockerfile must create /environment mountpoint for the catalog bind "
            "(campaign binds /environment/site.sqlite:ro)"
        )
