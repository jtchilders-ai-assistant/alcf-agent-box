#!/usr/bin/env python3
"""Create an attempt-local manifest for the ordered Polaris toolchain probes."""

import argparse
import json
import os
import pathlib
import tempfile


STAGES = [
    "cxx20_concepts",
    "mpi_native_two_rank",
    "mpi_gtl_link_resolution",
    "cuda_runtime_compatibility",
    "kokkos_required_features",
    "kokkos_cuda_production_rank",
    "pepper_configure_features",
]


def contained(path: pathlib.Path, root: pathlib.Path, label: str) -> pathlib.Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes attempt root: {resolved}") from exc
    return resolved


def atomic_json(path: pathlib.Path, payload: dict) -> None:
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt-root", required=True, type=pathlib.Path)
    parser.add_argument("--stage-probe", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args()

    root = args.attempt_root.resolve()
    if not root.is_dir():
        parser.error("attempt root must be an existing directory")
    probe = contained(args.stage_probe, root, "stage probe")
    output = contained(args.output, root, "manifest output")
    if not probe.is_file() or not os.access(str(probe), os.X_OK):
        parser.error("stage probe must be an executable regular file")

    profile = os.environ.get("RED_SHIRT_ENV_PROFILE_ID", "").strip()
    if not profile:
        parser.error("RED_SHIRT_ENV_PROFILE_ID is required")
    payload = {
        "schema_version": 1,
        "environment_profile_id": profile,
        "stages": [
            {"name": name, "command": [str(probe), name]}
            for name in STAGES
        ],
    }
    atomic_json(output, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
