#!/usr/bin/env python3
"""Generate attempt-local Red Shirt project context from observed facts.

The generated files contain operating instructions and non-secret environment
facts. They deliberately do not claim that an observed toolchain is compatible.
"""

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path


GENERATED_MARKER = "generated-by: red_shirt_task_context.py"
SECRET_KEY = re.compile(
    r"(?:^|_)(?:access_?token|refresh_?token|oauth_?token|client_?token|token|jwt|bearer|authorization|auth_?header|credential|credentials|password|passwd|secret|api_?key|auth_?key|private_?key|signing_?key|hmac_?key|token_?file|key_?file)(?:$|_)",
    re.IGNORECASE,
)
BEARER_VALUE = re.compile(r"(?i)\bbearer\s+\S+")


class ContextError(Exception):
    pass


def sanitize(value, key=""):
    """Return a JSON-compatible copy with credential fields redacted."""
    if SECRET_KEY.search(str(key)):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): sanitize(v, str(k)) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return BEARER_VALUE.sub("Bearer [REDACTED]", value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def markdown_value(value):
    if isinstance(value, list):
        return ", ".join(markdown_value(item) for item in value) or "(none observed)"
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def flatten(prefix, value):
    if isinstance(value, dict):
        for key in sorted(value):
            child = "%s.%s" % (prefix, key) if prefix else str(key)
            for item in flatten(child, value[key]):
                yield item
    else:
        yield prefix, value


def agents_document(task_root, facts):
    bridge = facts.get("bridge", {})
    actions = bridge.get("actions", []) if isinstance(bridge, dict) else []
    action_lines = "\n".join("- `%s`" % action for action in actions)
    if not action_lines:
        action_lines = "- No bridge actions were declared; stop and report this context defect."
    return """<!-- {marker} -->
# Red Shirt Attempt Contract

Task root: `{task_root}`

- Read `ENV.md` before acting. It records observed environment facts, not proof
  that an application will configure, compile, link, or run successfully.
- If `ENV.md` declares a `toolchain_preflight` manifest and runner, select and
  install candidate dependencies first, export the required `RED_SHIRT_*`
  paths for that exact candidate stack, then invoke the runner through the
  declared `run_script` bridge action. A failed or missing preflight blocks
  application configure/build/run success; preserve its complete evidence.

## Scope and ownership

- Work only inside the task root and the writable roots listed in `ENV.md`.
- You own application dependency discovery and installation, configuration,
  build, tests, execution, and scientific analysis.
- Infrastructure supplies the allocation and declared host bridge. Do not
  modify prior attempts, credentials, agent state, or PBS jobs unless the task
  explicitly authorizes it.

## Host bridge

Declared actions:
{action_lines}

Use only declared actions and their documented JSON schemas. Every request and
response must carry the same nonce. Preserve the script checksum, stdout/stderr
paths, start/end times, and exit code returned by the bridge. A bridge preflight
proves only the bridge boundary; it is not application success.

## Commands and time budget

- Terminal timeouts are seconds.
- A launch acknowledgement is not completion. If a command is promoted or
  backgrounded, wait for its exact process handle and never rerun it.
- Preserve raw output and exit status for every decisive command. Avoid
  pipelines that mask the decisive command's status.
- Update `STATUS.json` atomically after discovery, dependencies, configure,
  build, tests, run, analysis, and finalization.
- Reserve the final ten minutes. When that reserve begins, start no new build
  or simulation; write honest partial terminal artifacts instead.

## Evidence hierarchy

Keep these claims separate:

1. requested configuration;
2. detected configuration;
3. compiled/link evidence;
4. runtime evidence.

Contradictory evidence blocks success. Preserve the first unresolved failure.
Do not infer numerical results that are absent from retained raw output.

## Completion contract

Before exit, always write `REPORT.md` and `RESULT.json`, then write exactly one of `DONE` or `FAILED`.
`DONE` is permitted only when every task acceptance gate
has passed. Otherwise write `FAILED` with the current phase, first unresolved
failure, completed evidence, and next action.
""".format(marker=GENERATED_MARKER, task_root=task_root, action_lines=action_lines)


def env_document(facts):
    lines = [
        "<!-- %s -->" % GENERATED_MARKER,
        "# Red Shirt Attempt Environment",
        "",
        "**Observed facts; not compatibility proof.** Values below describe the",
        "prepared allocation and interfaces. Probe the coupled application stack",
        "before claiming it works.",
        "",
    ]
    for key, value in flatten("", facts):
        lines.append("- **%s:** `%s`" % (key, markdown_value(value).replace("`", "'")))
    lines.append("")
    return "\n".join(lines)


def initial_status():
    return {
        "schema_version": 1,
        "phase": "discovery",
        "state": "pending",
        "last_command": None,
        "last_exit_code": None,
        "evidence_paths": [],
        "first_unresolved_failure": None,
        "next_action": "Read AGENTS.md and ENV.md, then begin discovery.",
    }


def is_generated(path):
    try:
        return GENERATED_MARKER in path.read_text(encoding="utf-8")[:512]
    except (OSError, UnicodeError):
        return False


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".%s." % path.name, suffix=".tmp", dir=str(path.parent))
    stream = None
    try:
        os.fchmod(fd, 0o600)
        stream = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1
        with stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        stream = None
        os.replace(temp_name, str(path))
        os.chmod(str(path), 0o600)
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


def load_facts(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContextError("cannot read facts JSON: %s" % exc)
    if not isinstance(data, dict):
        raise ContextError("facts JSON must be a JSON object")
    return sanitize(data)


def generate(task_root, facts_path):
    if not task_root.is_absolute():
        raise ContextError("task root must be an absolute path")
    if not facts_path.is_absolute():
        raise ContextError("facts JSON path must be an absolute path")
    if not task_root.is_dir():
        raise ContextError("task root must be an existing directory")

    agents_path = task_root / "AGENTS.md"
    env_path = task_root / "ENV.md"
    status_path = task_root / "STATUS.json"

    for path in (agents_path, env_path):
        if path.exists() and not is_generated(path):
            raise ContextError("refusing to overwrite non-generated %s" % path)

    existing_status = None
    if status_path.exists():
        try:
            existing_status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ContextError("refusing to overwrite invalid STATUS.json: %s" % exc)
        if not isinstance(existing_status, dict) or existing_status.get("schema_version") != 1:
            raise ContextError("refusing to overwrite unrecognized STATUS.json")

    facts = load_facts(facts_path)
    # AGENTS.md activates the contract when Hermes starts in task_root. Publish
    # supporting state first and AGENTS.md last so a partial failure cannot
    # expose new instructions without their matching ENV/STATUS files.
    atomic_write(env_path, env_document(facts))
    if existing_status is None:
        atomic_write(status_path, json.dumps(initial_status(), indent=2, sort_keys=False) + "\n")
    else:
        os.chmod(str(status_path), 0o600)
    atomic_write(agents_path, agents_document(str(task_root), facts))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-root", required=True, type=Path)
    parser.add_argument("--facts-json", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        generate(args.task_root, args.facts_json)
    except ContextError as exc:
        print("red_shirt_task_context: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
