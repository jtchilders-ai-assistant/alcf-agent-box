import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
RUNNER = ROOT / "scripts" / "red_shirt_campaign.py"
CONTEXT = ROOT / "scripts" / "red_shirt_task_context.py"
PROBE = ROOT / "scripts" / "red_shirt_probe.py"


def executable(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def facts_file(path: Path, task: Path) -> Path:
    path.write_text(json.dumps({
        "pbs": {"job_id": "test.0", "nodes": 2, "ranks": 8},
        "agent": {"provider": "test-provider", "model": "test-model"},
        "bridge": {"client": "/campaign/red-shirt-host", "actions": ["env_report", "run_script", "run8"]},
        "writable_roots": [str(task)],
    }), encoding="utf-8")
    return path


def run_campaign(
    tmp_path: Path,
    *,
    probe_rc=0,
    probe_detail=None,
    token_rc=0,
    hermes_rc=0,
    hermes_artifacts=True,
    terminal_marker="DONE",
    overall_status="success",
    hermes_sleep=0,
    hermes_timeout=20,
):
    task = tmp_path / "task"
    runtime = tmp_path / "runtime"
    task.mkdir()
    runtime.mkdir()
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Perform the bounded scientific task.\n", encoding="utf-8")
    facts = facts_file(tmp_path / "facts.json", task)
    events = tmp_path / "events.log"
    secret = "secret-token-value-that-must-not-leak"

    token_helper = executable(tmp_path / "token-helper", f"""
import sys
assert sys.argv[1:] == ["get_access_token", "--service", "inference"]
if {token_rc} == 0:
    print({secret!r})
raise SystemExit({token_rc})
""")
    detail = probe_detail or ("success" if probe_rc == 0 else "unexpected HTTP status 401")
    probe = executable(tmp_path / "probe", f"""
import json, os, pathlib, sys
args=sys.argv[1:]
pathlib.Path({str(events)!r}).open("a").write("probe\\n")
token_path=pathlib.Path(args[args.index("--token-file")+1])
assert token_path.read_text().strip() == {secret!r}
print(json.dumps({{"step": "inference", "ok": {str(probe_rc == 0)}, "detail": {detail!r}}}))
raise SystemExit({probe_rc})
""")
    hermes_body = f"""
import json, os, pathlib, sys, time
root=pathlib.Path.cwd()
pathlib.Path({str(tmp_path / 'hermes.pid')!r}).write_text(str(os.getpid()))
time.sleep({hermes_sleep})
pathlib.Path({str(events)!r}).open("a").write("hermes\\n")
pathlib.Path({str(tmp_path / 'hermes-args.json')!r}).write_text(json.dumps(sys.argv[1:]))
assert (root / "AGENTS.md").is_file()
assert (root / "ENV.md").is_file()
assert (root / "STATUS.json").is_file()
"""
    if hermes_artifacts:
        hermes_body += f"""
(root / "REPORT.md").write_text("resident report\\n")
(root / "RESULT.json").write_text(json.dumps({{"overall_status":{overall_status!r}}})+"\\n")
(root / {terminal_marker!r}).write_text("\\n")
"""
    hermes_body += f"raise SystemExit({hermes_rc})\n"
    hermes = executable(tmp_path / "hermes", hermes_body)

    command = [
        sys.executable,
        str(RUNNER),
        "--task-root", str(task),
        "--runtime-root", str(runtime),
        "--facts-json", str(facts),
        "--prompt-file", str(prompt),
        "--context-generator", str(CONTEXT),
        "--token-helper", str(token_helper),
        "--probe", str(probe),
        "--base-url", "https://inference.invalid/v1",
        "--model", "test-model",
        "--proxy", "http://proxy.invalid:3128",
        "--hermes-bin", str(hermes),
        "--hermes-timeout", str(hermes_timeout),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    return result, task, runtime, events, secret


def test_campaign_refreshes_smokes_generates_context_then_runs_hermes(tmp_path):
    result, task, runtime, events, secret = run_campaign(tmp_path)

    assert result.returncode == 0, result.stderr
    assert events.read_text().splitlines() == ["probe", "hermes"]
    args = json.loads((tmp_path / "hermes-args.json").read_text())
    assert args[:3] == ["--yolo", "--in", str(task)]
    assert "-z" in args
    assert (task / "DONE").is_file()
    assert not (task / "FAILED").exists()
    combined = result.stdout + result.stderr + (runtime / "inference-smoke.json").read_text()
    assert secret not in combined
    assert not (runtime / "inference.token").exists()


def test_campaign_blocks_hermes_and_synthesizes_failure_when_smoke_fails(tmp_path):
    result, task, runtime, events, secret = run_campaign(tmp_path, probe_rc=1)

    assert result.returncode != 0
    assert events.read_text().splitlines() == ["probe"]
    assert not (tmp_path / "hermes-args.json").exists()
    assert (task / "FAILED").is_file()
    assert not (task / "DONE").exists()
    payload = json.loads((task / "RESULT.json").read_text())
    assert payload["overall_status"] == "failed"
    assert payload["generated_by"] == "red_shirt_campaign.py"
    assert payload["failure"]["kind"] == "inference_smoke_failed"
    assert secret not in (task / "RESULT.json").read_text()
    assert secret not in (task / "REPORT.md").read_text()


def test_campaign_classifies_real_probe_401_and_503_schema(tmp_path):
    for status, expected in ((401, "authorization"), (503, "unavailable")):
        case = tmp_path / str(status)
        case.mkdir()
        result, task, _runtime, _events, _secret = run_campaign(
            case,
            probe_rc=1,
            probe_detail=f"unexpected HTTP status {status}",
        )
        assert result.returncode != 0
        payload = json.loads((task / "RESULT.json").read_text())
        assert expected in payload["failure"]["message"].lower()


def test_campaign_blocks_hermes_when_token_refresh_fails(tmp_path):
    result, task, runtime, events, _secret = run_campaign(tmp_path, token_rc=9)

    assert result.returncode != 0
    assert not events.exists()
    assert not (tmp_path / "hermes-args.json").exists()
    assert not (runtime / "inference.token").exists()
    payload = json.loads((task / "RESULT.json").read_text())
    assert payload["failure"]["kind"] == "token_refresh_failed"
    assert payload["agent"]["exit_code"] is None


def test_campaign_times_out_hermes_and_synthesizes_failure(tmp_path):
    result, task, _runtime, _events, _secret = run_campaign(
        tmp_path,
        hermes_sleep=2,
        hermes_timeout=1,
        hermes_artifacts=False,
    )
    assert result.returncode != 0
    payload = json.loads((task / "RESULT.json").read_text())
    assert payload["failure"]["kind"] == "hermes_timeout"
    assert payload["agent"]["exit_code"] is None
    assert (task / "FAILED").is_file()
    pid = int((tmp_path / "hermes.pid").read_text())
    with __import__("pytest").raises(ProcessLookupError):
        os.kill(pid, 0)


def test_campaign_synthesizes_failure_when_hermes_exits_without_artifacts(tmp_path):
    result, task, runtime, _events, _secret = run_campaign(
        tmp_path, hermes_rc=1, hermes_artifacts=False
    )

    assert result.returncode != 0
    payload = json.loads((task / "RESULT.json").read_text())
    assert payload["failure"]["kind"] == "hermes_failed"
    assert payload["agent"]["exit_code"] == 1
    assert payload["artifacts_at_failure"]["REPORT.md"] is False
    assert payload["last_checkpoint"]["phase"] == "discovery"
    assert "wrapper-generated" in (task / "REPORT.md").read_text().lower()
    assert (task / "FAILED").is_file()


def test_campaign_converts_incomplete_zero_exit_to_failure(tmp_path):
    result, task, _runtime, _events, _secret = run_campaign(
        tmp_path, hermes_rc=0, hermes_artifacts=False
    )

    assert result.returncode != 0
    payload = json.loads((task / "RESULT.json").read_text())
    assert payload["failure"]["kind"] == "missing_terminal_artifacts"
    assert payload["agent"]["exit_code"] == 0
    assert (task / "FAILED").is_file()
    assert not (task / "DONE").exists()


def test_campaign_preserves_resident_report_on_nonzero_exit(tmp_path):
    result, task, _runtime, _events, _secret = run_campaign(
        tmp_path, hermes_rc=7, hermes_artifacts=True
    )

    assert result.returncode != 0
    assert (task / "REPORT.md").read_text() == "resident report\n"
    assert json.loads((task / "RESULT.json").read_text()) == {"overall_status": "success"}
    assert (task / "FAILED").is_file()
    assert not (task / "DONE").exists()
    wrapper = json.loads((task / "WRAPPER_RESULT.json").read_text())
    assert wrapper["failure"]["kind"] == "hermes_failed"


def test_campaign_accepts_honest_resident_failed_terminal_state(tmp_path):
    result, task, _runtime, _events, _secret = run_campaign(
        tmp_path, terminal_marker="FAILED", overall_status="failed"
    )

    assert result.returncode == 0, result.stderr
    assert (task / "FAILED").is_file()
    assert not (task / "DONE").exists()
    assert not (task / "WRAPPER_RESULT.json").exists()


def test_campaign_rejects_done_with_failed_result_status(tmp_path):
    result, task, _runtime, _events, _secret = run_campaign(
        tmp_path, terminal_marker="DONE", overall_status="failed"
    )

    assert result.returncode != 0
    assert (task / "FAILED").is_file()
    assert not (task / "DONE").exists()
    wrapper = json.loads((task / "WRAPPER_RESULT.json").read_text())
    assert wrapper["failure"]["kind"] == "invalid_terminal_artifacts"
