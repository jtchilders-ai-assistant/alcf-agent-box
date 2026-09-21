import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
CLIENT = ROOT / "scripts" / "red_shirt_host_client.sh"
WATCHER = ROOT / "scripts" / "red_shirt_host_watcher.sh"
WORKER = ROOT / "scripts" / "red_shirt_host_bridge.sh"
RANK_WRAPPER = ROOT / "scripts" / "red_shirt_rank_wrapper.sh"
GPU_PROBE = ROOT / "scripts" / "red_shirt_gpu_rank_probe.py"


def write_executable(path: Path, body: str):
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body, encoding="utf-8")
    path.chmod(0o755)


def run_client(tmp_path, action="env_report", request=None, worker_body=None, worker_env=None):
    attempt = tmp_path / "attempt"
    bridge = attempt / "bridge"
    output = attempt / "output"
    bridge.mkdir(parents=True)
    output.mkdir()
    worker = tmp_path / "worker.sh"
    write_executable(worker, worker_body or f'exec "{WORKER}"\n')
    fake_tools = tmp_path / "fake-tools"
    fake_tools.mkdir()
    write_executable(fake_tools / "flock", "exit 0\n")
    watcher_env = os.environ.copy()
    watcher_env["PATH"] = str(fake_tools) + os.pathsep + watcher_env.get("PATH", "")
    watcher_env.update({
        "RED_SHIRT_BRIDGE_DIR": str(bridge),
        "RED_SHIRT_HOST_BRIDGE": str(worker),
    })
    watcher_env.update(worker_env or {})
    watcher = subprocess.Popen(
        ["bash", str(WATCHER)],
        env=watcher_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(100):
            if (bridge / "READY").exists():
                break
            if watcher.poll() is not None:
                error = watcher.stderr.read() if watcher.stderr is not None else "watcher exited"
                raise AssertionError(error)
            __import__("time").sleep(0.01)
        payload = request or {"output_dir": str(output)}
        env = os.environ.copy()
        env["RED_SHIRT_BRIDGE_DIR"] = str(bridge)
        result = subprocess.run(
            ["bash", str(CLIENT), action, json.dumps(payload)],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        response = json.loads((bridge / "response.json").read_text())
        return result, response, output
    finally:
        (bridge / "STOP").touch()
        watcher.wait(timeout=5)


def test_bridge_scripts_exist_and_have_valid_shell():
    for path in (CLIENT, WATCHER, WORKER, RANK_WRAPPER):
        assert path.is_file(), path
        result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    assert GPU_PROBE.is_file()
    result = subprocess.run(
        ["python3", "-m", "py_compile", str(GPU_PROBE)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_env_report_returns_correlated_structured_stage_record(tmp_path):
    result, response, output = run_client(tmp_path)

    assert result.returncode == 0, result.stderr
    assert response["version"] == 1
    assert response["action"] == "env_report"
    assert response["nonce"]
    assert response["exit_code"] == 0
    assert response["started_at"] <= response["finished_at"]
    assert response["environment_profile_id"]
    assert response["stdout_path"] == str(output / "env_report.stdout")
    assert response["stderr_path"] == str(output / "env_report.stderr")
    assert response["script_sha256"] is None
    assert (output / "environment.json").is_file()


def test_run_script_records_checksum_and_exit_code(tmp_path):
    attempt = tmp_path / "attempt"
    script = attempt / "task" / "step.sh"
    script.parent.mkdir(parents=True)
    write_executable(script, "echo hello\nexit 7\n")
    output = attempt / "output"
    request = {"script": str(script), "output_dir": str(output)}

    result, response, output = run_client(tmp_path, "run_script", request)

    assert result.returncode == 7
    assert response["exit_code"] == 7
    assert len(response["script_sha256"]) == 64
    assert Path(response["stdout_path"]).read_text() == "hello\n"
    assert Path(response["stderr_path"]).is_file()


def test_bridge_rejects_paths_outside_attempt_root(tmp_path):
    outside = tmp_path.parent / "outside.sh"
    write_executable(outside, "exit 0\n")
    output = tmp_path / "attempt" / "output"
    result, response, _ = run_client(
        tmp_path,
        "run_script",
        {"script": str(outside), "output_dir": str(output)},
    )
    assert result.returncode != 0
    assert response["exit_code"] != 0
    assert response["bridge_error"] is True
    request_payload = json.loads(
        (tmp_path / "attempt" / "bridge" / "request.json").read_text()
    )
    assert response["action"] == request_payload["action"]
    assert response["nonce"] == request_payload["nonce"]


def test_run8_executes_only_the_operator_configured_launcher(tmp_path):
    attempt = tmp_path / "attempt"
    executable_path = attempt / "bin" / "simulation"
    executable_path.parent.mkdir(parents=True)
    write_executable(executable_path, "exit 0\n")
    output = attempt / "output"
    launcher = tmp_path / "run8-launcher.sh"
    write_executable(
        launcher,
        'printf "%s\\n" "$RED_SHIRT_EXECUTABLE" > "$RED_SHIRT_OUTPUT_DIR/observed-executable"\n',
    )
    result, response, output = run_client(
        tmp_path,
        "run8",
        {"executable": str(executable_path), "output_dir": str(output)},
        worker_env={"RED_SHIRT_RUN8_LAUNCHER": str(launcher)},
    )

    assert result.returncode == 0, result.stderr
    assert response["exit_code"] == 0
    assert (output / "observed-executable").read_text().strip() == str(executable_path)


def test_run8_rejects_executable_outside_attempt_root(tmp_path):
    outside = tmp_path.parent / "outside-simulation"
    write_executable(outside, "exit 0\n")
    output = tmp_path / "attempt" / "output"
    result, response, _ = run_client(
        tmp_path,
        "run8",
        {"executable": str(outside), "output_dir": str(output)},
    )
    assert result.returncode != 0
    assert response["bridge_error"] is True


def test_watcher_synthesizes_correlated_response_when_worker_crashes(tmp_path):
    result, response, _ = run_client(
        tmp_path,
        worker_body="exit 23\n",
    )
    assert result.returncode == 23
    assert response["exit_code"] == 23
    assert response["bridge_error"] is True
    assert response["nonce"]


def test_client_rejects_reserved_protocol_fields(tmp_path):
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    env = os.environ.copy()
    env["RED_SHIRT_BRIDGE_DIR"] = str(bridge)
    result = subprocess.run(
        ["bash", str(CLIENT), "env_report", json.dumps({"nonce": "forged"})],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert not (bridge / "request.json").exists()


def test_bridge_rejects_unknown_request_fields_with_correlation(tmp_path):
    result, response, _ = run_client(
        tmp_path,
        "env_report",
        {"output_dir": str(tmp_path / "attempt" / "output"), "shell_payload": "bad"},
    )
    request_payload = json.loads(
        (tmp_path / "attempt" / "bridge" / "request.json").read_text()
    )
    assert result.returncode != 0
    assert response["bridge_error"] is True
    assert response["action"] == request_payload["action"]
    assert response["nonce"] == request_payload["nonce"]


def test_bridge_fails_closed_when_lock_utility_is_unavailable(tmp_path):
    bridge = tmp_path / "attempt" / "bridge"
    bridge.mkdir(parents=True)
    request = {
        "version": 1,
        "action": "env_report",
        "nonce": "lock-test",
        "output_dir": str(tmp_path / "attempt" / "output"),
    }
    (bridge / "request.json").write_text(json.dumps(request))
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    env = os.environ.copy()
    env.update({
        "RED_SHIRT_BRIDGE_DIR": str(bridge),
        "PATH": str(empty_path),
    })
    result = subprocess.run(
        ["/bin/bash", str(WORKER)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "flock is required" in result.stderr


def test_client_refuses_a_second_inflight_request(tmp_path):
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    (bridge / "client.lock").mkdir()
    env = os.environ.copy()
    env["RED_SHIRT_BRIDGE_DIR"] = str(bridge)
    result = subprocess.run(
        ["bash", str(CLIENT), "env_report", "{}"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 75
    assert "already active" in result.stderr
    assert not (bridge / "request.json").exists()


def test_watcher_terminates_and_reaps_inflight_worker(tmp_path):
    bridge = tmp_path / "attempt" / "bridge"
    bridge.mkdir(parents=True)
    worker = tmp_path / "worker.sh"
    worker_pid = tmp_path / "worker.pid"
    write_executable(
        worker,
        f'printf "%s\\n" "$$" > {str(worker_pid)!r}\nexec sleep 30\n',
    )
    env = os.environ.copy()
    env.update({"RED_SHIRT_BRIDGE_DIR": str(bridge), "RED_SHIRT_HOST_BRIDGE": str(worker)})
    watcher = subprocess.Popen(["bash", str(WATCHER)], env=env)
    for _ in range(100):
        if (bridge / "READY").exists():
            break
        __import__("time").sleep(0.01)
    (bridge / "request.json").write_text(json.dumps({
        "version": 1, "action": "env_report", "nonce": "terminate-test"
    }))
    for _ in range(100):
        if worker_pid.exists():
            break
        __import__("time").sleep(0.01)
    assert worker_pid.exists()
    watcher.terminate()
    assert watcher.wait(timeout=5) != 0
    pid = int(worker_pid.read_text())
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    else:
        raise AssertionError(f"worker {pid} remained alive")


def test_empty_stop_sentinel_stops_watcher(tmp_path):
    bridge = tmp_path / "attempt" / "bridge"
    bridge.mkdir(parents=True)
    worker = tmp_path / "worker.sh"
    write_executable(worker, "exit 0\n")
    env = os.environ.copy()
    env.update({"RED_SHIRT_BRIDGE_DIR": str(bridge), "RED_SHIRT_HOST_BRIDGE": str(worker)})
    watcher = subprocess.Popen(["bash", str(WATCHER)], env=env)
    for _ in range(100):
        if (bridge / "READY").exists():
            break
        __import__("time").sleep(0.01)
    (bridge / "STOP").touch()
    assert watcher.wait(timeout=5) == 0


def test_rank_wrapper_allows_explicit_gpu_count_and_rejects_invalid_value():
    text = RANK_WRAPPER.read_text()
    assert "RED_SHIRT_GPUS_PER_NODE" in text
    assert "GPUS_PER_NODE must be a positive integer" in text


def test_gpu_probe_reports_missing_device_assignment_cleanly(tmp_path):
    env = os.environ.copy()
    env.pop("CUDA_VISIBLE_DEVICES", None)
    result = subprocess.run(
        ["python3", str(GPU_PROBE)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "CUDA_VISIBLE_DEVICES is not set" in result.stderr
    assert "Traceback" not in result.stderr


def test_bridge_helpers_are_packaged_in_the_resident_image():
    text = (ROOT / "Dockerfile.red-shirt-polaris").read_text()
    for name in (
        "red_shirt_host_bridge.sh",
        "red_shirt_host_client.sh",
        "red_shirt_host_watcher.sh",
        "red_shirt_rank_wrapper.sh",
        "red_shirt_gpu_rank_probe.py",
    ):
        assert f"COPY scripts/{name} /opt/red-shirt-polaris/{name}" in text
