import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
GENERATOR = ROOT / "scripts" / "red_shirt_task_context.py"


def load_generator_module():
    spec = importlib.util.spec_from_file_location("red_shirt_task_context", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_generator(task_root: Path, facts_path: Path):
    return subprocess.run(
        [
            sys.executable,
            str(GENERATOR),
            "--task-root",
            str(task_root),
            "--facts-json",
            str(facts_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )


def write_facts(path: Path, **updates):
    facts = {
        "pbs": {
            "job_id": "1234.polaris-pbs-01",
            "remaining_walltime": "00:42:00",
            "nodefile": "/opt/data/campaign/run/pbs_nodefile",
            "nodefile_sha256": "a" * 64,
            "nodes": 2,
            "ranks": 8,
            "gpus_per_node": 4,
        },
        "image": {
            "sif_path": "/opt/data/images/red-shirt.sif",
            "source_digest": "sha256:" + "b" * 64,
            "sif_sha256": "c" * 64,
            "revision": "2d31b43",
        },
        "agent": {
            "hermes_version": "2026.9.14",
            "provider": "alcf-minerva-reasoning",
            "model": "inkling-bf16",
            "python": "3.11.9",
        },
        "toolchain": {
            "profile_id": "gnu14-cuda13-kokkos-probe-pending",
            "compiler": "CC backed by GNU 14",
            "mpi": "Cray MPICH; coupled probe pending",
            "cuda": "CUDA runtime compatibility pending",
            "kokkos": "feature compatibility pending",
            "modules": ["PrgEnv-gnu", "cray-mpich", "cuda"],
        },
        "bridge": {
            "client": "/opt/data/campaign-tools/red-shirt-host",
            "actions": ["env_report", "run_script", "run8"],
            "timeout_units": "seconds",
        },
        "network": {"public_egress": "ALCF HTTP(S) proxy required"},
        "writable_roots": [str(path.parent / "task")],
    }
    facts.update(updates)
    path.write_text(json.dumps(facts), encoding="utf-8")


def test_generator_creates_attempt_context_atomically_and_mode_safely(tmp_path):
    task = tmp_path / "task"
    task.mkdir()
    facts = tmp_path / "facts.json"
    write_facts(facts)

    result = run_generator(task, facts)

    assert result.returncode == 0, result.stderr
    agents = (task / "AGENTS.md").read_text(encoding="utf-8")
    env = (task / "ENV.md").read_text(encoding="utf-8")
    status = json.loads((task / "STATUS.json").read_text(encoding="utf-8"))

    assert "generated-by: red_shirt_task_context.py" in agents
    assert f"Task root: `{task}`" in agents
    for phrase in (
        "Read `ENV.md` before acting",
        "timeouts are seconds",
        "never rerun it",
        "requested configuration",
        "detected configuration",
        "compiled/link evidence",
        "runtime evidence",
        "STATUS.json",
        "REPORT.md",
        "RESULT.json",
        "exactly one of `DONE` or `FAILED`",
        "ten minutes",
    ):
        assert phrase.lower() in agents.lower(), phrase
    for action in ("env_report", "run_script", "run8"):
        assert action in agents

    assert "Observed facts; not compatibility proof" in env
    for value in (
        "1234.polaris-pbs-01",
        "sha256:" + "b" * 64,
        "inkling-bf16",
        "gnu14-cuda13-kokkos-probe-pending",
        "/opt/data/campaign-tools/red-shirt-host",
    ):
        assert value in env

    assert status == {
        "schema_version": 1,
        "phase": "discovery",
        "state": "pending",
        "last_command": None,
        "last_exit_code": None,
        "evidence_paths": [],
        "first_unresolved_failure": None,
        "next_action": "Read AGENTS.md and ENV.md, then begin discovery.",
    }
    for output in (task / "AGENTS.md", task / "ENV.md", task / "STATUS.json"):
        assert os.stat(output).st_mode & 0o777 == 0o600
    assert not list(task.glob(".*.tmp"))


def test_generator_refuses_to_overwrite_human_agents_file(tmp_path):
    task = tmp_path / "task"
    task.mkdir()
    agents = task / "AGENTS.md"
    agents.write_text("human-owned rules\n", encoding="utf-8")
    facts = tmp_path / "facts.json"
    write_facts(facts)

    result = run_generator(task, facts)

    assert result.returncode != 0
    assert "refusing to overwrite" in result.stderr.lower()
    assert agents.read_text(encoding="utf-8") == "human-owned rules\n"
    assert not (task / "ENV.md").exists()
    assert not (task / "STATUS.json").exists()


def test_generator_updates_its_own_files_but_preserves_progress(tmp_path):
    task = tmp_path / "task"
    task.mkdir()
    facts = tmp_path / "facts.json"
    write_facts(facts)
    assert run_generator(task, facts).returncode == 0
    status_path = task / "STATUS.json"
    status = json.loads(status_path.read_text())
    status.update({"phase": "build", "state": "running", "last_exit_code": 0})
    status_path.write_text(json.dumps(status), encoding="utf-8")

    result = run_generator(task, facts)

    assert result.returncode == 0, result.stderr
    assert json.loads(status_path.read_text())["phase"] == "build"
    assert json.loads(status_path.read_text())["state"] == "running"


def test_generator_redacts_secret_fields_and_values(tmp_path):
    task = tmp_path / "task"
    task.mkdir()
    facts = tmp_path / "facts.json"
    secret = "do-not-leak-this-bearer-value"
    write_facts(
        facts,
        credentials=secret,
        access_token=secret,
        token_file="/mnt/secrets/inference.token",
        private_key=secret,
        jwt=secret,
        note=f"authorization header Bearer {secret}",
    )

    result = run_generator(task, facts)

    assert result.returncode == 0, result.stderr
    combined = "\n".join(
        (task / name).read_text(encoding="utf-8")
        for name in ("AGENTS.md", "ENV.md", "STATUS.json")
    )
    assert secret not in combined
    assert "/mnt/secrets/inference.token" not in combined
    assert combined.count("[REDACTED]") >= 5


def test_generator_publishes_agents_last(tmp_path, monkeypatch):
    module = load_generator_module()
    task = tmp_path / "task"
    task.mkdir()
    facts = tmp_path / "facts.json"
    write_facts(facts)
    published = []
    real_atomic_write = module.atomic_write

    def record_write(path, content):
        published.append(path.name)
        real_atomic_write(path, content)

    monkeypatch.setattr(module, "atomic_write", record_write)
    module.generate(task, facts)

    assert published == ["ENV.md", "STATUS.json", "AGENTS.md"]


def test_generator_fails_closed_for_invalid_or_relative_inputs(tmp_path):
    task = tmp_path / "task"
    task.mkdir()
    facts = tmp_path / "facts.json"
    facts.write_text("[]", encoding="utf-8")
    result = run_generator(task, facts)
    assert result.returncode != 0
    assert "json object" in result.stderr.lower()

    facts.write_text("{}", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--task-root", "relative/task", "--facts-json", str(facts)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "absolute" in result.stderr.lower()


def test_atomic_write_closes_descriptor_when_fchmod_fails(tmp_path, monkeypatch):
    module = load_generator_module()
    real_close = module.os.close
    closed = []

    def fail_fchmod(_fd, _mode):
        raise OSError("simulated fchmod failure")

    def record_close(fd):
        closed.append(fd)
        return real_close(fd)

    monkeypatch.setattr(module.os, "fchmod", fail_fchmod)
    monkeypatch.setattr(module.os, "close", record_close)
    target = tmp_path / "AGENTS.md"

    try:
        module.atomic_write(target, "content")
    except OSError as exc:
        assert "simulated fchmod failure" in str(exc)
    else:
        raise AssertionError("atomic_write unexpectedly succeeded")

    assert len(closed) == 1
    assert not target.exists()
    assert not list(tmp_path.glob(".AGENTS.md.*.tmp"))


# ---------------------------------------------------------------------------
# Task 5: Resident context includes catalog discovery guidance
# ---------------------------------------------------------------------------

def write_facts_with_catalog(path: Path, **updates):
    """Write a facts.json that includes an environment_catalog entry."""
    import json as _json
    facts = {
        "pbs": {
            "job_id": "5678.polaris-pbs-01",
            "remaining_walltime": "00:55:00",
            "nodefile": "/opt/data/campaign/run/pbs_nodefile",
            "nodefile_sha256": "d" * 64,
            "nodes": 2,
            "ranks": 8,
            "gpus_per_node": 4,
        },
        "image": {
            "sif_path": "/opt/data/images/red-shirt.sif",
            "source_digest": "sha256:" + "e" * 64,
            "sif_sha256": "f" * 64,
            "revision": "abc1234",
        },
        "agent": {
            "hermes_version": "2026.9.21",
            "provider": "alcf-minerva",
            "model": "probe-model-bf16",
            "python": "3.11.9",
        },
        "toolchain": {
            "profile_id": "gnu14-catalog-test",
            "compiler": "CC backed by GNU 14",
            "mpi": "Cray MPICH",
            "cuda": "CUDA 12.4",
            "kokkos": "4.4.01",
            "modules": ["PrgEnv-gnu", "cray-mpich", "cuda"],
        },
        "bridge": {
            "client": "/opt/data/campaign-tools/red-shirt-host",
            "actions": ["env_report", "run_script", "run8"],
            "timeout_units": "seconds",
        },
        "network": {"public_egress": "ALCF HTTP(S) proxy required"},
        "writable_roots": [str(path.parent / "task")],
        "environment_catalog": {
            "site_db": "/opt/attempt/environment/site.sqlite",
            "site_sha256": "a" * 64,
            "overlay": "/opt/attempt/environment-overlay",
            "query_cli": "/opt/red-shirt-polaris/red_shirt_env_catalog.py",
            "collection_status": "complete",
            "epistemic_status": "discovery_only_not_compatibility_proof",
        },
    }
    facts.update(updates)
    path.write_text(_json.dumps(facts), encoding="utf-8")


def test_agents_document_explains_catalog_discovery(tmp_path):
    """AGENTS.md must instruct Red Shirt on catalog use for discovery."""
    task = tmp_path / "task"
    task.mkdir()
    facts = tmp_path / "facts.json"
    write_facts_with_catalog(facts)

    result = run_generator(task, facts)

    assert result.returncode == 0, result.stderr
    agents = (task / "AGENTS.md").read_text(encoding="utf-8")

    for phrase in (
        "catalog",
        "discovery",
        "provenance",
        "discovery_only_not_compatibility_proof",
    ):
        assert phrase.lower() in agents.lower(), (
            f"AGENTS.md must mention '{phrase}' when environment_catalog is in facts"
        )


def test_agents_document_explains_catalog_freshness_and_completeness(tmp_path):
    """AGENTS.md must mention freshness/completeness caveats for the catalog."""
    task = tmp_path / "task"
    task.mkdir()
    facts = tmp_path / "facts.json"
    write_facts_with_catalog(facts)

    result = run_generator(task, facts)

    assert result.returncode == 0, result.stderr
    agents = (task / "AGENTS.md").read_text(encoding="utf-8")

    for phrase in ("freshness", "completeness", "snapshot"):
        assert phrase.lower() in agents.lower(), (
            f"AGENTS.md must mention catalog '{phrase}' limitations"
        )


def test_agents_document_preserves_contradictions_instruction(tmp_path):
    """AGENTS.md must tell Red Shirt to preserve contradictory catalog records."""
    task = tmp_path / "task"
    task.mkdir()
    facts = tmp_path / "facts.json"
    write_facts_with_catalog(facts)

    result = run_generator(task, facts)

    assert result.returncode == 0, result.stderr
    agents = (task / "AGENTS.md").read_text(encoding="utf-8")

    assert "contradict" in agents.lower() or "conflict" in agents.lower(), (
        "AGENTS.md must instruct Red Shirt to preserve (not erase) contradictory records"
    )


def test_agents_document_instructs_structured_observations(tmp_path):
    """AGENTS.md must tell Red Shirt to record structured attempt observations."""
    task = tmp_path / "task"
    task.mkdir()
    facts = tmp_path / "facts.json"
    write_facts_with_catalog(facts)

    result = run_generator(task, facts)

    assert result.returncode == 0, result.stderr
    agents = (task / "AGENTS.md").read_text(encoding="utf-8")

    assert "observation" in agents.lower() or "overlay" in agents.lower(), (
        "AGENTS.md must instruct Red Shirt to record structured observations in the overlay"
    )


def test_agents_document_compile_link_runtime_proof_remains_authoritative(tmp_path):
    """AGENTS.md must make clear that compiled/link/runtime proof is authoritative."""
    task = tmp_path / "task"
    task.mkdir()
    facts = tmp_path / "facts.json"
    write_facts_with_catalog(facts)

    result = run_generator(task, facts)

    assert result.returncode == 0, result.stderr
    agents = (task / "AGENTS.md").read_text(encoding="utf-8")

    # Evidence hierarchy must persist even with catalog guidance added.
    for phrase in ("compiled/link evidence", "runtime evidence"):
        assert phrase.lower() in agents.lower(), (
            f"AGENTS.md must retain evidence hierarchy phrase: '{phrase}'"
        )


def test_agents_document_catalog_guidance_absent_when_no_catalog_in_facts(tmp_path):
    """Catalog guidance section must not appear when facts have no environment_catalog."""
    task = tmp_path / "task"
    task.mkdir()
    facts = tmp_path / "facts.json"
    # write_facts (from conftest/top) has no environment_catalog
    write_facts(facts)

    result = run_generator(task, facts)

    assert result.returncode == 0, result.stderr
    agents = (task / "AGENTS.md").read_text(encoding="utf-8")

    # When there is no catalog, the epistemic marker phrase must not appear
    # (would be confusing/misleading to mention it without the catalog)
    assert "discovery_only_not_compatibility_proof" not in agents, (
        "Catalog epistemic marker must not appear in AGENTS.md when no catalog is in facts"
    )
