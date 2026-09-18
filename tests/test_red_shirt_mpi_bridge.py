#!/usr/bin/env python3
"""Contract tests for Red Shirt's direct Polaris host-MPI bridge."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PBS = ROOT / "deploy" / "polaris" / "red-shirt-polaris.pbs"
MPI_ENV = ROOT / "scripts" / "red_shirt_mpi_env.sh"
ACCEPTANCE = ROOT / "deploy" / "polaris" / "red-shirt-mpi-acceptance.pbs"
README = ROOT / "deploy" / "polaris" / "RED_SHIRT_README.md"


def text(path: Path) -> str:
    assert path.is_file(), f"missing {path.relative_to(ROOT)}"
    return path.read_text(encoding="utf-8")


def test_host_mpi_environment_helper_exists_and_is_valid_shell():
    body = text(MPI_ENV)
    proc = subprocess.run(["bash", "-n", str(MPI_ENV)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "ml cray-mpich-abi" in body
    assert "CRAY_LD_LIBRARY_PATH" in body
    assert "command -v mpiexec" in body
    assert "PBS_NODEFILE" in body
    assert "/run/palsd" in body and "/var/run/palsd" in body


def test_launcher_sources_host_mpi_environment_before_apptainer():
    body = text(PBS)
    source_at = body.index("red_shirt_mpi_env.sh")
    run_at = body.index("apptainer run")
    assert source_at < run_at


def test_launcher_preserves_exact_pbs_hostfile_and_checksum():
    body = text(PBS)
    assert re.search(r'test\s+.*-f\s+"\$PBS_NODEFILE"', body)
    assert 'install -m 600 "$PBS_NODEFILE" "$RUN_DIR/pbs_nodefile"' in body
    assert 'sha256sum "$RUN_DIR/pbs_nodefile"' in body
    assert 'APPTAINERENV_RED_SHIRT_HOSTFILE="/opt/data/runs/${JOB_ID}/pbs_nodefile"' in body
    assert 'APPTAINERENV_PBS_NODEFILE="$APPTAINERENV_RED_SHIRT_HOSTFILE"' in body
    assert not re.search(r"x\d+c\d+s\d+b\d+n\d+", body)


def test_launcher_exposes_resolved_host_mpi_environment():
    body = text(PBS)
    for name in (
        "APPTAINERENV_PATH",
        "APPTAINERENV_LD_LIBRARY_PATH",
        "APPTAINERENV_CRAY_LD_LIBRARY_PATH",
        "APPTAINERENV_RED_SHIRT_HOST_MPIEXEC",
    ):
        assert name in body


def test_launcher_binds_required_host_trees_read_only():
    body = " ".join(text(PBS).split())
    for binding in (
        '"/opt/cray:/opt/cray:ro"',
        '"/opt/nvidia:/opt/nvidia:ro"',
        '"/opt/cray/libfabric:/opt/cray/libfabric:ro"',
        '"/soft:/soft:ro"',
    ):
        assert binding in body
    assert '"$PALS_RUNTIME_DIR:$PALS_RUNTIME_DIR:ro"' in body


def test_acceptance_job_is_two_node_and_uses_exact_hostfile():
    body = text(ACCEPTANCE)
    assert "#PBS -l select=2:system=polaris" in body
    assert "#PBS -q debug" in body
    assert re.search(r"#PBS -l walltime=00:[123]0:00", body)
    assert 'install -m 600 "$PBS_NODEFILE"' in body
    assert '--hostfile "$PBS_NODEFILE"' in body
    assert re.search(r"(?:-n|--np)\s+2\b", body)
    assert re.search(r"--ppn\s+1\b", body)
    assert "module list" in body
    assert "ldd" in body
    assert "apptainer exec" in body
    assert "terminal.json" in body


def test_runbook_documents_hostfile_and_direct_allocation_access():
    body = text(README)
    for token in (
        "PBS_NODEFILE",
        "pbs_nodefile",
        "cray-mpich-abi",
        "mpiexec",
        "/opt/cray",
        "packaging boundary",
        "multiple isolated",
    ):
        assert token.lower() in body.lower(), token
