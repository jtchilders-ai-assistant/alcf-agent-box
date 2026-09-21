#!/usr/bin/env python3
"""Prepare and run one bounded Red Shirt campaign attempt."""

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path


REQUIRED_ARTIFACTS = ("REPORT.md", "RESULT.json")
TERMINAL_MARKERS = ("DONE", "FAILED")


class CampaignError(Exception):
    def __init__(self, kind, message, exit_code=1, agent_exit=None):
        super().__init__(message)
        self.kind = kind
        self.exit_code = exit_code
        self.agent_exit = agent_exit


def atomic_write(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".%s." % path.name, suffix=".tmp", dir=str(path.parent))
    stream = None
    try:
        os.fchmod(fd, mode)
        stream = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1
        with stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        stream = None
        os.replace(temp_name, str(path))
        os.chmod(str(path), mode)
    except Exception:
        if stream is not None and not stream.closed:
            stream.close()
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def artifact_state(task_root):
    return {name: (task_root / name).is_file() for name in REQUIRED_ARTIFACTS + TERMINAL_MARKERS}


def load_status(task_root):
    path = task_root / "STATUS.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {
            "phase": data.get("phase"),
            "state": data.get("state"),
            "last_command": data.get("last_command"),
            "last_exit_code": data.get("last_exit_code"),
            "first_unresolved_failure": data.get("first_unresolved_failure"),
            "evidence_paths": data.get("evidence_paths", []),
            "next_action": data.get("next_action"),
        }
    except (OSError, ValueError, TypeError):
        return {"phase": "unknown", "state": "unknown"}


def classify_probe_failure(probe_path):
    try:
        data = json.loads(probe_path.read_text(encoding="utf-8"))
        detail = data.get("detail", "")
        if not isinstance(detail, str):
            detail = ""
        if "HTTP status 401" in detail:
            return "inference_smoke_failed", "Inference authorization was rejected (HTTP 401)."
        if "HTTP status 503" in detail:
            return "inference_smoke_failed", "Inference endpoint was unavailable (HTTP 503)."
    except (OSError, ValueError, TypeError):
        pass
    return "inference_smoke_failed", "The pre-campaign inference smoke failed."


def synthesize_failure(task_root, runtime_root, kind, message, agent_exit=None):
    before = artifact_state(task_root)
    payload = {
        "schema_version": 1,
        "overall_status": "failed",
        "generated_by": "red_shirt_campaign.py",
        "failure": {"kind": kind, "message": message},
        "agent": {"exit_code": agent_exit},
        "last_checkpoint": load_status(task_root),
        "artifacts_at_failure": before,
        "runtime_evidence": {
            "agent_stdout": str(runtime_root / "agent.stdout"),
            "agent_stderr": str(runtime_root / "agent.stderr"),
            "inference_smoke": str(runtime_root / "inference-smoke.json"),
        },
    }
    atomic_write(
        task_root / "WRAPPER_RESULT.json",
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    if not before["RESULT.json"]:
        atomic_write(task_root / "RESULT.json", json.dumps(payload, indent=2, sort_keys=True) + "\n")
    if not before["REPORT.md"]:
        atomic_write(
            task_root / "REPORT.md",
            "# Wrapper-generated failure report\n\n"
            "This report was wrapper-generated because the resident did not leave a report.\n\n"
            "- Failure kind: `%s`\n- Detail: %s\n- Last checkpoint: `%s`\n"
            % (kind, message, payload["last_checkpoint"].get("phase", "unknown")),
        )
    # Publish failure before removing a contradictory success marker. A monitor
    # may briefly observe both markers, which is explicitly invalid and causes
    # it to wait/re-read; it must never observe a terminal task with no marker.
    atomic_write(task_root / "FAILED", "wrapper-generated\n")
    (task_root / "DONE").unlink(missing_ok=True)


def run_checked(command, **kwargs):
    try:
        return subprocess.run(command, check=False, **kwargs)
    except OSError as exc:
        raise CampaignError("launcher_error", str(exc))


def run(args):
    task_root = args.task_root.resolve()
    runtime_root = args.runtime_root.resolve()
    facts_json = args.facts_json.resolve()
    prompt_file = args.prompt_file.resolve()
    for path, label in ((task_root, "task root"), (runtime_root, "runtime root")):
        if not path.is_dir():
            raise CampaignError("invalid_input", "%s must be an existing directory" % label, 2)
    for path, label in ((facts_json, "facts JSON"), (prompt_file, "prompt file")):
        if not path.is_file():
            raise CampaignError("invalid_input", "%s must be a readable file" % label, 2)

    context = run_checked(
        [sys.executable, str(args.context_generator), "--task-root", str(task_root), "--facts-json", str(facts_json)],
        capture_output=True,
        text=True,
    )
    if context.returncode:
        raise CampaignError("context_generation_failed", context.stderr.strip() or "context generation failed")

    token_path = runtime_root / "inference.token"
    smoke_path = runtime_root / "inference-smoke.json"
    try:
        token_fd = os.open(str(token_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(token_fd, "w", encoding="utf-8") as stream:
            token = run_checked(
                [str(args.token_helper), "get_access_token", "--service", "inference"],
                stdout=stream,
                stderr=subprocess.PIPE,
                text=True,
            )
        if token.returncode:
            raise CampaignError("token_refresh_failed", "Could not obtain a fresh inference token.")

        with smoke_path.open("w", encoding="utf-8") as smoke:
            probe = run_checked(
                [
                    str(args.probe),
                    "inference",
                    "--base-url", args.base_url,
                    "--model", args.model,
                    "--token-file", str(token_path),
                    "--proxy", args.proxy,
                ],
                stdout=smoke,
                stderr=subprocess.STDOUT,
                text=True,
            )
        if probe.returncode:
            kind, message = classify_probe_failure(smoke_path)
            raise CampaignError(kind, message)
    finally:
        token_path.unlink(missing_ok=True)

    prompt = prompt_file.read_text(encoding="utf-8")
    with (runtime_root / "agent.stdout").open("w", encoding="utf-8") as stdout, \
            (runtime_root / "agent.stderr").open("w", encoding="utf-8") as stderr:
        command = [str(args.hermes_bin), "--yolo", "--in", str(task_root), "-z", prompt]
        try:
            process = subprocess.Popen(
                command,
                cwd=str(task_root),
                stdout=stdout,
                stderr=stderr,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            raise CampaignError("launcher_error", str(exc))
        try:
            returncode = process.wait(timeout=args.hermes_timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise CampaignError(
                "hermes_timeout",
                "Hermes exceeded the configured timeout of %s seconds." % args.hermes_timeout,
            )
        agent = subprocess.CompletedProcess(command, returncode)

    state = artifact_state(task_root)
    complete = state["REPORT.md"] and state["RESULT.json"] and (state["DONE"] != state["FAILED"])
    if agent.returncode:
        raise CampaignError("hermes_failed", "Hermes exited nonzero.", agent.returncode, agent.returncode)
    if not complete:
        raise CampaignError(
            "missing_terminal_artifacts",
            "Hermes exited without the complete terminal artifact contract.",
            agent_exit=agent.returncode,
        )
    try:
        result_payload = json.loads((task_root / "RESULT.json").read_text(encoding="utf-8"))
        overall_status = result_payload.get("overall_status")
    except (OSError, ValueError, TypeError) as exc:
        raise CampaignError(
            "invalid_terminal_artifacts",
            "RESULT.json is invalid: %s" % exc,
            agent_exit=agent.returncode,
        )
    if state["DONE"] and overall_status != "success":
        raise CampaignError(
            "invalid_terminal_artifacts",
            "DONE requires RESULT.json overall_status=success.",
            agent_exit=agent.returncode,
        )
    if state["FAILED"] and overall_status == "success":
        raise CampaignError(
            "invalid_terminal_artifacts",
            "FAILED contradicts RESULT.json overall_status=success.",
            agent_exit=agent.returncode,
        )
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-root", required=True, type=Path)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--facts-json", required=True, type=Path)
    parser.add_argument("--prompt-file", required=True, type=Path)
    parser.add_argument("--context-generator", required=True, type=Path)
    parser.add_argument("--token-helper", required=True, type=Path)
    parser.add_argument("--probe", required=True, type=Path)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--proxy", required=True)
    parser.add_argument("--hermes-bin", required=True, type=Path)
    parser.add_argument("--hermes-timeout", required=True, type=int)
    args = parser.parse_args(argv)
    if args.hermes_timeout <= 0:
        parser.error("--hermes-timeout must be a positive integer")
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        return run(args)
    except CampaignError as exc:
        try:
            task_root = args.task_root.resolve()
            runtime_root = args.runtime_root.resolve()
            if task_root.is_dir() and runtime_root.is_dir():
                synthesize_failure(task_root, runtime_root, exc.kind, str(exc), exc.agent_exit)
        except Exception as synthesis_error:
            print("red_shirt_campaign: failure synthesis failed: %s" % synthesis_error, file=sys.stderr)
        print("red_shirt_campaign: %s" % exc, file=sys.stderr)
        return exc.exit_code if exc.exit_code else 1


if __name__ == "__main__":
    sys.exit(main())
