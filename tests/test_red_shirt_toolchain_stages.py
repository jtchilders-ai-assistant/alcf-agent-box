import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
PROBE = ROOT / "scripts" / "red_shirt_toolchain_stage.sh"
MANIFEST = ROOT / "scripts" / "red_shirt_toolchain_manifest.py"
STAGES = [
    "cxx20_concepts",
    "mpi_native_two_rank",
    "mpi_gtl_link_resolution",
    "cuda_runtime_compatibility",
    "kokkos_required_features",
    "kokkos_cuda_production_rank",
    "pepper_configure_features",
]


def test_stage_probe_and_manifest_generator_are_syntactically_valid():
    assert subprocess.run(["bash", "-n", str(PROBE)]).returncode == 0
    assert subprocess.run(["python3", "-m", "py_compile", str(MANIFEST)]).returncode == 0


def test_manifest_generator_emits_exact_order_and_attempt_local_commands(tmp_path):
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    staged_probe = attempt / "red_shirt_toolchain_stage.sh"
    staged_probe.write_text(PROBE.read_text())
    staged_probe.chmod(0o755)
    output = attempt / "manifest.json"

    env = dict(__import__("os").environ)
    env["RED_SHIRT_ENV_PROFILE_ID"] = "fixture-profile"
    result = subprocess.run(
        ["python3", str(MANIFEST), "--attempt-root", str(attempt),
         "--stage-probe", str(staged_probe), "--output", str(output)],
        env=env, capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(output.read_text())
    assert [stage["name"] for stage in payload["stages"]] == STAGES
    assert payload["environment_profile_id"] == "fixture-profile"
    for stage in payload["stages"]:
        assert stage["command"] == [str(staged_probe), stage["name"]]


def test_manifest_generator_requires_named_environment_profile(tmp_path):
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    staged_probe = attempt / "red_shirt_toolchain_stage.sh"
    staged_probe.write_text(PROBE.read_text())
    staged_probe.chmod(0o755)
    output = attempt / "manifest.json"
    env = dict(__import__("os").environ)
    env.pop("RED_SHIRT_ENV_PROFILE_ID", None)

    result = subprocess.run(
        ["python3", str(MANIFEST), "--attempt-root", str(attempt),
         "--stage-probe", str(staged_probe), "--output", str(output)],
        env=env, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "RED_SHIRT_ENV_PROFILE_ID" in result.stderr
    assert not output.exists()


def test_probe_requires_explicit_selected_stack_inputs():
    text = PROBE.read_text()
    for variable in (
        "RED_SHIRT_MPICXX",
        "RED_SHIRT_MPIEXEC",
        "RED_SHIRT_PBS_NODEFILE",
        "RED_SHIRT_KOKKOS_PREFIX",
        "RED_SHIRT_PEPPER_SOURCE",
        "RED_SHIRT_PRODUCTION_RUNNER",
        "RED_SHIRT_ENV_PROFILE_ID",
        "RED_SHIRT_EXPECTED_RANKS",
        "RED_SHIRT_EXPECTED_HOSTS",
    ):
        assert variable in text
    assert "module load" not in text
    assert "ml load" not in text
    assert "Kokkos 4.6.02" not in text
    assert "cuda/12.9" not in text


def test_probe_contains_real_stage_acceptance_operations():
    text = PROBE.read_text()
    required = (
        "#include <concepts>",
        "MPI_Init",
        "-n 2",
        "ldd",
        "not found",
        "libcudart",
        "Kokkos_ENABLE_CUDA",
        "Kokkos_ENABLE_CUDA_LAMBDA",
        "Kokkos_ENABLE_CUDA_CONSTEXPR",
        "Kokkos_ARCH_AMPERE80",
        "Kokkos::kokkos",
        "Kokkos::parallel_reduce",
        "MPI_PROBE rank=%d size=%d host=%s",
        "KOKKOS_PROBE rank=%d host=%s value=%ld",
        "len(hosts) == 2",
        "KOKKOS_ENABLE_CUDA",
        "KOKKOS_ENABLE_CUDA_LAMBDA",
        "KOKKOS_ENABLE_CUDA_CONSTEXPR",
        "KOKKOS_ARCH_AMPERE80",
        "mpi_gtl_link_resolution/mpi_link_probe",
        "PEPPER_MPI_DISABLED",
        "CMakeCache.txt",
    )
    for token in required:
        assert token in text, token


def test_unknown_stage_is_rejected_without_side_effects(tmp_path):
    env = {
        "PATH": "/usr/bin:/bin",
        "RED_SHIRT_PROBE_WORKDIR": str(tmp_path),
    }
    result = subprocess.run(
        ["bash", str(PROBE), "not_a_stage"], env=env,
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "unsupported toolchain stage" in result.stderr


def test_probe_bundle_is_packaged_in_resident_image():
    text = (ROOT / "Dockerfile.red-shirt-polaris").read_text()
    assert "COPY scripts/red_shirt_toolchain_stage.sh /opt/red-shirt-polaris/red_shirt_toolchain_stage.sh" in text
    assert "COPY scripts/red_shirt_toolchain_manifest.py /opt/red-shirt-polaris/red_shirt_toolchain_manifest.py" in text
