#!/usr/bin/env python3
"""Run the ordered Red Shirt coupled-toolchain acceptance contract."""

import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Sequence


REQUIRED_STAGES = [
    "cxx20_concepts",
    "mpi_native_two_rank",
    "mpi_gtl_link_resolution",
    "cuda_runtime_compatibility",
    "kokkos_required_features",
    "kokkos_cuda_production_rank",
    "pepper_configure_features",
]


def atomic_json(path: pathlib.Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    stream = None
    try:
        os.fchmod(fd, 0o600)
        stream = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1
        with stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        stream = None
        os.replace(temporary, path)
    except BaseException:
        if stream is not None and not stream.closed:
            stream.close()
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def load_manifest(path: pathlib.Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("unsupported preflight manifest")
    stages = payload.get("stages")
    if not isinstance(stages, list) or [stage.get("name") for stage in stages] != REQUIRED_STAGES:
        raise ValueError("stages do not match the ordered coupled-stack contract")
    root = path.parent.resolve()
    for stage in stages:
        command = stage.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
            raise ValueError(f"stage {stage.get('name')} command must be a nonempty argv array")
        executable = pathlib.Path(command[0]).resolve()
        try:
            executable.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"stage command escapes manifest directory: {executable}") from exc
        if not executable.is_file():
            raise ValueError(f"stage command is not a file: {executable}")
    profile = payload.get("environment_profile_id")
    if not isinstance(profile, str) or not profile:
        raise ValueError("environment_profile_id is required")
    return payload


def command_hash(command: Sequence[str]) -> str:
    encoded = json.dumps(list(command), separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def run(manifest_path: pathlib.Path, output_dir: pathlib.Path) -> int:
    manifest = load_manifest(manifest_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    results: List[Dict[str, Any]] = []
    first_failed = None
    return_code = 0

    for stage in manifest["stages"]:
        name = stage["name"]
        command = stage["command"]
        stdout_path = output_dir / f"{name}.stdout"
        stderr_path = output_dir / f"{name}.stderr"
        record: Dict[str, Any] = {
            "name": name,
            "status": "not_run",
            "command": command,
            "command_sha256": command_hash(command),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
        }
        if first_failed is not None:
            results.append(record)
            continue
        record["started_at"] = time.time()
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            completed = subprocess.run(command, cwd=str(manifest_path.parent), stdout=stdout, stderr=stderr)
        record["finished_at"] = time.time()
        record["exit_code"] = completed.returncode
        record["status"] = "passed" if completed.returncode == 0 else "failed"
        results.append(record)
        if completed.returncode != 0:
            first_failed = name
            return_code = completed.returncode

    summary = {
        "schema_version": 1,
        "environment_profile_id": manifest["environment_profile_id"],
        "overall_status": "passed" if first_failed is None else "failed",
        "first_failed_stage": first_failed,
        "stages": results,
    }
    atomic_json(output_dir / "toolchain-preflight.json", summary)
    return return_code


def main(argv: Sequence[str] = ()) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=pathlib.Path)
    parser.add_argument("--output-dir", required=True, type=pathlib.Path)
    args = parser.parse_args(argv or None)
    try:
        return run(args.manifest.resolve(), args.output_dir.resolve())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"preflight error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
