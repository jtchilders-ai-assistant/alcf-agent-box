import importlib.util
import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
PREFLIGHT = ROOT / "scripts" / "red_shirt_toolchain_preflight.py"
STAGES = [
    "cxx20_concepts",
    "mpi_native_two_rank",
    "mpi_gtl_link_resolution",
    "cuda_runtime_compatibility",
    "kokkos_required_features",
    "kokkos_cuda_production_rank",
    "pepper_configure_features",
]


def load_preflight_module():
    spec = importlib.util.spec_from_file_location("red_shirt_toolchain_preflight", PREFLIGHT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_executable(path: Path, body: str):
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body, encoding="utf-8")
    path.chmod(0o755)


def make_probe_fixture(tmp_path: Path, fail_stage=None):
    probes = tmp_path / "probes"
    output = tmp_path / "evidence"
    probes.mkdir()
    for stage in STAGES:
        exit_code = 17 if stage == fail_stage else 0
        write_executable(
            probes / f"{stage}.sh",
            f'printf "%s\\n" "evidence for {stage}"\nexit {exit_code}\n',
        )
    manifest = {
        "schema_version": 1,
        "environment_profile_id": "fixture-profile",
        "stages": [
            {"name": stage, "command": [str(probes / f"{stage}.sh")]}
            for stage in STAGES
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path, output


def run_preflight(manifest: Path, output: Path):
    return subprocess.run(
        ["python3", str(PREFLIGHT), "--manifest", str(manifest), "--output-dir", str(output)],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_preflight_records_ordered_coupled_stack_evidence(tmp_path):
    manifest, output = make_probe_fixture(tmp_path)
    result = run_preflight(manifest, output)

    assert result.returncode == 0, result.stderr
    summary = json.loads((output / "toolchain-preflight.json").read_text())
    assert summary["overall_status"] == "passed"
    assert summary["environment_profile_id"] == "fixture-profile"
    assert [stage["name"] for stage in summary["stages"]] == STAGES
    assert all(stage["status"] == "passed" for stage in summary["stages"])
    for stage in summary["stages"]:
        assert Path(stage["stdout_path"]).is_file()
        assert Path(stage["stderr_path"]).is_file()
        assert stage["command"]
        assert len(stage["command_sha256"]) == 64


def test_preflight_fails_closed_and_does_not_run_later_stages(tmp_path):
    failed_stage = "cuda_runtime_compatibility"
    manifest, output = make_probe_fixture(tmp_path, fail_stage=failed_stage)
    result = run_preflight(manifest, output)

    assert result.returncode == 17
    summary = json.loads((output / "toolchain-preflight.json").read_text())
    statuses = {stage["name"]: stage["status"] for stage in summary["stages"]}
    assert summary["overall_status"] == "failed"
    assert summary["first_failed_stage"] == failed_stage
    assert statuses[failed_stage] == "failed"
    assert statuses["kokkos_required_features"] == "not_run"
    assert statuses["kokkos_cuda_production_rank"] == "not_run"
    assert statuses["pepper_configure_features"] == "not_run"


def test_preflight_rejects_missing_or_reordered_contract_stages(tmp_path):
    manifest, output = make_probe_fixture(tmp_path)
    payload = json.loads(manifest.read_text())
    payload["stages"] = list(reversed(payload["stages"]))
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = run_preflight(manifest, output)

    assert result.returncode != 0
    assert "ordered coupled-stack contract" in result.stderr
    assert not (output / "toolchain-preflight.json").exists()


def test_preflight_rejects_commands_outside_the_manifest_tree(tmp_path):
    manifest, output = make_probe_fixture(tmp_path)
    outside = tmp_path.parent / "outside-probe.sh"
    write_executable(outside, "exit 0\n")
    payload = json.loads(manifest.read_text())
    payload["stages"][0]["command"] = [str(outside)]
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = run_preflight(manifest, output)

    assert result.returncode != 0
    assert "escapes manifest directory" in result.stderr


def test_atomic_json_closes_descriptor_when_fdopen_fails(tmp_path, monkeypatch):
    module = load_preflight_module()
    real_close = module.os.close
    closed = []

    def fail_fdopen(_fd, *_args, **_kwargs):
        raise OSError("simulated fdopen failure")

    def record_close(fd):
        closed.append(fd)
        return real_close(fd)

    monkeypatch.setattr(module.os, "fdopen", fail_fdopen)
    monkeypatch.setattr(module.os, "close", record_close)
    target = tmp_path / "result.json"

    try:
        module.atomic_json(target, {"status": "test"})
    except OSError as exc:
        assert "simulated fdopen failure" in str(exc)
    else:
        raise AssertionError("atomic_json unexpectedly succeeded")

    assert len(closed) == 1
    assert not target.exists()


def test_toolchain_preflight_is_packaged_in_the_resident_image():
    text = (ROOT / "Dockerfile.red-shirt-polaris").read_text()
    assert (
        "COPY scripts/red_shirt_toolchain_preflight.py "
        "/opt/red-shirt-polaris/red_shirt_toolchain_preflight.py"
    ) in text
