#!/usr/bin/env python3
"""
red_shirt_env_catalog.py — Secret-safe, provenance-preserving SQLite catalog
of Polaris modules, software paths, dependency edges, and attempt observations.

Standard-library only. Python 3.8-compatible.

Commands:
  init                 Create a new site catalog (writable, status=open).
  finalize             Run integrity checks, chmod 0444, write .sha256 sidecar.
  verify               Check sidecar digest, schema, integrity, complete status.
  collect              Bounded host collection of module/path/elf provenance.
  search               FTS5 search over entities.
  show                 Show a single entity with evidence level.
  dependencies         Directed dependency closure from a named entity.
  reverse-dependencies Reverse dependency closure.
  status               Print snapshot metadata as JSON.
  observe              Record a structured observation into a writable overlay.
  insert-test-entity   Test helper: insert a safe entity (used by tests only).
  insert-test-relation Test helper: insert a relation (used by tests only).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SCHEMA_VERSION = 1
TOOL_VERSION = "1.0.0"

# Secret firewall -----------------------------------------------------------
_SECRET_KEY_PATTERNS = re.compile(
    r"(?i)(token|password|passwd|secret|credential|api[-_]?key|private[-_]?key"
    r"|authorization|auth[-_]?token|access[-_]?token|client[-_]?secret"
    r"|bearer|x-api-key|x-auth-token)",
)

_SECRET_VALUE_PATTERNS = [
    # Bearer tokens
    re.compile(r"(?i)bearer\s+[a-z0-9\-._~+/]+=*"),
    # URL with userinfo (user:pass@host)
    re.compile(r"https?://[^@\s]+:[^@\s]+@"),
    # PEM headers
    re.compile(r"-----BEGIN [A-Z ]*(PRIVATE|RSA|EC|DSA)[^-]*KEY-----"),
    # JWT-shaped strings (three base64url segments separated by dots, starts with eyJ)
    re.compile(r"^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$"),
    # Canary / test sentinel patterns: _CANARY_ or _SECRET_ in value
    re.compile(r"(?i)_canary_|_secret_"),
]

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY_PATTERNS.search(key))


def _is_secret_value(value: str) -> bool:
    if not isinstance(value, str):
        return False
    for pat in _SECRET_VALUE_PATTERNS:
        if pat.search(value):
            return True
    return False


def _validate_string(key: str, value: str) -> None:
    """Raise ValueError if key or value looks secret. Message never includes value."""
    if _is_secret_key(key):
        raise ValueError(f"Rejected: key '{key}' matches secret pattern")
    if _SHA256_RE.match(value):
        return  # SHA-256 digests are explicitly safe
    if _is_secret_value(value):
        raise ValueError(f"Rejected: value for key '{key}' matches secret pattern")


def _validate_metadata(metadata: Dict[str, Any]) -> None:
    """Recursively validate a metadata dict for secret-like keys/values."""
    for k, v in metadata.items():
        if _is_secret_key(str(k)):
            raise ValueError(f"Rejected: key '{k}' matches secret pattern")
        if isinstance(v, dict):
            _validate_metadata(v)
        elif isinstance(v, str):
            if _is_secret_key(k) or _is_secret_value(v):
                raise ValueError(f"Rejected: key '{k}' contains secret-like content")


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------
_DDL = """
PRAGMA journal_mode=DELETE;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id                TEXT PRIMARY KEY,
    system            TEXT NOT NULL,
    source_id         TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'open'
                          CHECK(status IN ('open','complete')),
    collection_status TEXT NOT NULL DEFAULT 'pending'
                          CHECK(collection_status IN ('pending','complete','incomplete')),
    created_at        TEXT NOT NULL,
    finalized_at      TEXT
);

CREATE TABLE IF NOT EXISTS entities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    version     TEXT,
    kind        TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 0,
    evidence_level TEXT NOT NULL DEFAULT 'declared',
    source_kind TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE(snapshot_id, name, kind)
);

CREATE TABLE IF NOT EXISTS relations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    from_entity TEXT NOT NULL,
    to_entity   TEXT NOT NULL,
    kind        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id         TEXT REFERENCES snapshots(id) ON DELETE CASCADE,
    subject             TEXT NOT NULL,
    kind                TEXT NOT NULL,
    claim               TEXT,
    evidence_level      TEXT NOT NULL,
    outcome             TEXT NOT NULL,
    evidence_path       TEXT,
    evidence_sha256     TEXT,
    provenance_sha256   TEXT,
    command_exit_code   INTEGER,
    env_profile_id      TEXT,
    source_kind         TEXT,
    created_at          TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS entity_fts USING fts5(
    name,
    version,
    kind,
    content='entities',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS entities_ai AFTER INSERT ON entities BEGIN
    INSERT INTO entity_fts(rowid, name, version, kind)
    VALUES (new.id, new.name, COALESCE(new.version,''), new.kind);
END;

CREATE TRIGGER IF NOT EXISTS entities_ad AFTER DELETE ON entities BEGIN
    INSERT INTO entity_fts(entity_fts, rowid, name, version, kind)
    VALUES ('delete', old.id, old.name, COALESCE(old.version,''), old.kind);
END;

CREATE TRIGGER IF NOT EXISTS entities_au AFTER UPDATE ON entities BEGIN
    INSERT INTO entity_fts(entity_fts, rowid, name, version, kind)
    VALUES ('delete', old.id, old.name, COALESCE(old.version,''), old.kind);
    INSERT INTO entity_fts(rowid, name, version, kind)
    VALUES (new.id, new.name, COALESCE(new.version,''), new.kind);
END;
"""

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    """Return UTC timestamp in ISO-8601 format ending with Z."""
    t = time.gmtime()
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", t)


def _new_id() -> str:
    import uuid
    return str(uuid.uuid4())


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _open_rw(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.execute("PRAGMA foreign_keys=ON")
    con.row_factory = sqlite3.Row
    return con


def _open_ro(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path}?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True)
    con.execute("PRAGMA query_only=ON")
    con.row_factory = sqlite3.Row
    return con


def _get_snapshot_id(con: sqlite3.Connection) -> str:
    row = con.execute("SELECT id FROM snapshots LIMIT 1").fetchone()
    if not row:
        raise RuntimeError("No snapshot found")
    return row["id"]


# ---------------------------------------------------------------------------
# Command: init
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    # Write to a temp file in the same directory; atomically replace
    tmp_fd, tmp_name = tempfile.mkstemp(
        suffix=".tmp.sqlite", dir=str(output.parent)
    )
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)

    try:
        con = sqlite3.connect(str(tmp_path))
        con.executescript(_DDL)

        snapshot_id = _new_id()
        now = _utc_now()
        con.execute(
            "INSERT INTO snapshots (id, system, source_id, status, collection_status, created_at)"
            " VALUES (?, ?, ?, 'open', 'pending', ?)",
            (snapshot_id, args.system, args.source_id, now),
        )
        con.execute(
            "INSERT INTO metadata (key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
        con.execute(
            "INSERT INTO metadata (key, value) VALUES (?, ?)",
            ("tool_version", TOOL_VERSION),
        )
        con.execute(
            "INSERT INTO metadata (key, value) VALUES (?, ?)",
            ("created_at", now),
        )
        con.commit()
        con.close()
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    os.replace(tmp_name, str(output))
    os.chmod(str(output), 0o600)
    return 0


# ---------------------------------------------------------------------------
# Command: finalize
# ---------------------------------------------------------------------------

def cmd_finalize(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"error: database not found: {db_path}", file=sys.stderr)
        return 1

    # Check current mode — must be writable
    mode = stat_mode(db_path)
    if mode & 0o222 == 0:
        print("error: database already finalized (read-only)", file=sys.stderr)
        return 1

    con = _open_rw(db_path)
    try:
        # Check not already finalized
        row = con.execute("SELECT status FROM snapshots").fetchone()
        if row and row["status"] == "complete":
            con.close()
            print("error: snapshot already finalized", file=sys.stderr)
            return 1

        # Integrity checks
        fk_rows = con.execute("PRAGMA foreign_key_check").fetchall()
        if fk_rows:
            con.close()
            print(f"error: foreign key violations: {fk_rows}", file=sys.stderr)
            return 1

        ic = con.execute("PRAGMA integrity_check").fetchone()
        if ic and ic[0] != "ok":
            con.close()
            print(f"error: integrity check failed: {ic[0]}", file=sys.stderr)
            return 1

        fts_ic = con.execute("INSERT INTO entity_fts(entity_fts) VALUES('integrity-check')").fetchone()

        now = _utc_now()
        con.execute(
            "UPDATE snapshots SET status='complete', finalized_at=?",
            (now,),
        )
        # Mark collection_status complete if still pending
        con.execute(
            "UPDATE snapshots SET collection_status='complete'"
            " WHERE collection_status='pending'",
        )
        con.commit()
        con.close()
    except Exception as e:
        con.close()
        print(f"error: finalize failed: {e}", file=sys.stderr)
        return 1

    # Write SHA-256 sidecar
    digest = _sha256_file(db_path)
    sidecar = Path(str(db_path) + ".sha256")
    sidecar.write_text(f"{digest}  {db_path.name}\n")

    # Chmod both to 0444
    os.chmod(str(db_path), 0o444)
    os.chmod(str(sidecar), 0o444)
    return 0


def stat_mode(path: Path) -> int:
    import stat as statmod
    return statmod.S_IMODE(os.stat(path).st_mode)


# ---------------------------------------------------------------------------
# Command: verify
# ---------------------------------------------------------------------------

def cmd_verify(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    sidecar = Path(str(db_path) + ".sha256")

    if not db_path.exists():
        print("error: database not found", file=sys.stderr)
        return 1

    if not sidecar.exists():
        print("error: .sha256 sidecar not found", file=sys.stderr)
        return 1

    # Parse sidecar
    parts = sidecar.read_text().strip().split()
    if len(parts) < 1:
        print("error: malformed sidecar", file=sys.stderr)
        return 1
    expected_digest = parts[0]
    actual_digest = _sha256_file(db_path)
    if actual_digest != expected_digest:
        print(
            f"error: digest mismatch (expected {expected_digest[:16]}..., "
            f"got {actual_digest[:16]}...)",
            file=sys.stderr,
        )
        return 1

    # Open read-only and verify status
    try:
        con = _open_ro(db_path)
        row = con.execute("SELECT status FROM snapshots").fetchone()
        if not row or row["status"] != "complete":
            con.close()
            print("error: snapshot is not finalized (status != complete)", file=sys.stderr)
            return 1

        sv = con.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()
        if not sv or int(sv[0]) != SCHEMA_VERSION:
            con.close()
            print("error: schema version mismatch", file=sys.stderr)
            return 1

        ic = con.execute("PRAGMA integrity_check").fetchone()
        con.close()
        if ic and ic[0] != "ok":
            print(f"error: integrity check: {ic[0]}", file=sys.stderr)
            return 1
    except Exception as e:
        print(f"error: verify failed: {e}", file=sys.stderr)
        return 1

    print("ok")
    return 0


# ---------------------------------------------------------------------------
# Command: insert-test-entity (test helper)
# ---------------------------------------------------------------------------

def cmd_insert_test_entity(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    try:
        metadata = json.loads(args.metadata)
    except (json.JSONDecodeError, AttributeError) as e:
        print(f"error: invalid metadata JSON: {e}", file=sys.stderr)
        return 1

    key = metadata.get("key", "")
    value = metadata.get("value", "")

    # Secret firewall — validate key and value, and nested content
    try:
        _validate_string(key, str(value))
        if isinstance(value, dict):
            _validate_metadata(value)
        elif isinstance(value, str):
            # Also check if the value is JSON containing secrets
            try:
                nested = json.loads(value)
                if isinstance(nested, dict):
                    _validate_metadata(nested)
            except json.JSONDecodeError:
                pass  # not JSON, already checked as plain string above
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    con = _open_rw(db_path)
    try:
        snapshot_id = _get_snapshot_id(con)
        now = _utc_now()

        # Derive name/version from value
        val_str = str(value)
        if "/" in val_str:
            parts = val_str.split("/", 1)
            name, version = parts[0], parts[1]
        else:
            name, version = val_str, None

        con.execute(
            "INSERT OR IGNORE INTO entities"
            " (snapshot_id, name, version, kind, active, evidence_level, source_kind, created_at)"
            " VALUES (?, ?, ?, ?, 0, 'declared', ?, ?)",
            (snapshot_id, name, version, key, None, now),
        )
        con.commit()
    except Exception as e:
        con.rollback()
        con.close()
        print(f"error: {e}", file=sys.stderr)
        return 1

    con.close()
    return 0


# ---------------------------------------------------------------------------
# Command: insert-test-relation (test helper)
# ---------------------------------------------------------------------------

def cmd_insert_test_relation(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    con = _open_rw(db_path)
    try:
        snapshot_id = _get_snapshot_id(con)
        now = _utc_now()
        con.execute(
            "INSERT INTO relations (snapshot_id, from_entity, to_entity, kind, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (snapshot_id, args.from_entity, args.to_entity, args.kind, now),
        )
        con.commit()
    except Exception as e:
        con.rollback()
        con.close()
        print(f"error: {e}", file=sys.stderr)
        return 1
    con.close()
    return 0


# ---------------------------------------------------------------------------
# Command: collect
# ---------------------------------------------------------------------------

_SAFE_MODULE_DIRECTIVE_RE = re.compile(
    r"^\s*(prepend-path|append-path|setenv|module-whatis|prereq|conflict|load|unload)"
    r"\s+(\S+)(?:\s+(.*))?$"
)
_ALLOWED_ENV_VARS_FROM_SHOW = {"PATH", "LD_LIBRARY_PATH", "MANPATH", "PKG_CONFIG_PATH",
                               "CPATH", "INCLUDE", "LIBRARY_PATH", "LD_RUN_PATH"}
_ELF_TAG_RE = re.compile(r"\((NEEDED|RPATH|RUNPATH)\)\s+(?:Shared library:|Library (?:rpath|runpath):)\s+\[(.+?)\]")


def _module_cmd(env: Dict) -> str:
    return env.get("RED_SHIRT_MODULE_CMD", "module")


def _which_cmd(env: Dict) -> str:
    return env.get("RED_SHIRT_WHICH_CMD", "which")


def _readelf_cmd(env: Dict) -> str:
    return env.get("RED_SHIRT_READELF_CMD", "readelf")


def _run_bounded(cmd: List[str], timeout: int, env: Dict) -> Tuple[int, str]:
    """Run a command (no shell=True) with timeout. Returns (returncode, stdout_lines)."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return proc.returncode, proc.stdout
    except subprocess.TimeoutExpired:
        return 124, ""  # 124 = timeout exit code convention
    except FileNotFoundError:
        return 127, ""


def _sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _sha256_cmd(cmd: List[str]) -> str:
    return _sha256_str(" ".join(cmd))


def cmd_collect(args: argparse.Namespace) -> int:
    # Validate bounds
    timeout = args.command_timeout
    if timeout <= 0:
        print("error: --command-timeout must be positive", file=sys.stderr)
        return 1

    if hasattr(args, "module_limit") and args.module_limit is not None:
        if args.module_limit <= 0:
            print("error: --module-limit must be positive", file=sys.stderr)
            return 1

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    # Create the catalog first
    init_args = argparse.Namespace(
        output=str(output),
        system=args.system,
        source_id=args.source_id,
    )
    rc = cmd_init(init_args)
    if rc != 0:
        return rc

    con = _open_rw(output)
    snapshot_id = _get_snapshot_id(con)

    # Build env for subprocesses (pass through PATH only — no raw env dump)
    subprocess_env = {}
    path_val = os.environ.get("PATH", "")
    if path_val:
        subprocess_env["PATH"] = path_val
    # Preserve module command overrides for testing
    for override_key in ("RED_SHIRT_MODULE_CMD", "RED_SHIRT_WHICH_CMD", "RED_SHIRT_READELF_CMD"):
        if override_key in os.environ:
            subprocess_env[override_key] = os.environ[override_key]

    module_cmd = _module_cmd(subprocess_env)
    which_cmd = _which_cmd(subprocess_env)
    readelf_cmd = _readelf_cmd(subprocess_env)

    collection_incomplete = False

    # --- Collect from LOADEDMODULES (after secret validation) ---
    loaded = os.environ.get("LOADEDMODULES", "")
    if loaded:
        try:
            _validate_string("LOADEDMODULES_value", loaded)
            active_modules = [m.strip() for m in loaded.split(":") if m.strip()]
            for mod_spec in active_modules:
                try:
                    _validate_string("module_spec", mod_spec)
                except ValueError:
                    continue
                parts = mod_spec.split("/", 1)
                name = parts[0]
                version = parts[1] if len(parts) > 1 else None
                now = _utc_now()
                try:
                    con.execute(
                        "INSERT OR IGNORE INTO entities"
                        " (snapshot_id, name, version, kind, active, evidence_level,"
                        " source_kind, created_at)"
                        " VALUES (?, ?, ?, 'module', 1, 'declared', 'loadedmodules', ?)",
                        (snapshot_id, name, version, now),
                    )
                except sqlite3.IntegrityError:
                    pass
        except ValueError:
            pass  # LOADEDMODULES itself was secret-like — skip

    # --- Collect requested modules ---
    requested_modules = list(args.module) if args.module else []

    for mod_spec in requested_modules:
        try:
            _validate_string("module_spec", mod_spec)
        except ValueError as e:
            print(f"warning: skipping module (secret-like): {e}", file=sys.stderr)
            continue

        parts = mod_spec.split("/", 1)
        mod_name = parts[0]
        mod_version = parts[1] if len(parts) > 1 else None
        now = _utc_now()

        # Insert module entity
        try:
            con.execute(
                "INSERT OR IGNORE INTO entities"
                " (snapshot_id, name, version, kind, active, evidence_level, source_kind, created_at)"
                " VALUES (?, ?, ?, 'module', 0, 'declared', 'requested', ?)",
                (snapshot_id, mod_name, mod_version, now),
            )
        except sqlite3.IntegrityError:
            pass

        # Run module show
        show_cmd = [module_cmd, "show", mod_spec]
        rc, stdout = _run_bounded(show_cmd, timeout, subprocess_env)
        cmd_sha = _sha256_cmd(show_cmd)
        src_kind = "module_show"

        now = _utc_now()
        con.execute(
            "INSERT INTO observations"
            " (snapshot_id, subject, kind, claim, evidence_level, outcome,"
            " provenance_sha256, command_exit_code, source_kind, created_at)"
            " VALUES (?, ?, 'probe', 'module show', 'declared', ?, ?, ?, ?, ?)",
            (snapshot_id, mod_spec,
             "success" if rc == 0 else "failure",
             cmd_sha, rc, src_kind, now),
        )

        if rc != 0:
            collection_incomplete = True
            continue

        # Parse module show output — only allowlisted directives
        for line in stdout.splitlines():
            m = _SAFE_MODULE_DIRECTIVE_RE.match(line)
            if not m:
                continue
            directive, field, rest = m.group(1), m.group(2), m.group(3) or ""
            rest = rest.strip()

            if directive in ("prepend-path", "append-path"):
                if field in _ALLOWED_ENV_VARS_FROM_SHOW and rest:
                    if rest.startswith("/"):
                        try:
                            _validate_string("path", rest)
                            now = _utc_now()
                            con.execute(
                                "INSERT OR IGNORE INTO entities"
                                " (snapshot_id, name, version, kind, active,"
                                " evidence_level, source_kind, created_at)"
                                " VALUES (?, ?, NULL, 'path', 0, 'declared', 'module_show', ?)",
                                (snapshot_id, rest, now),
                            )
                        except ValueError:
                            pass

            elif directive in ("prereq", "load"):
                # field is the required module name; rest may have additional args
                dep_spec = (rest.split()[0] if rest else field)
                try:
                    _validate_string("dep_spec", dep_spec)
                    now = _utc_now()
                    con.execute(
                        "INSERT INTO relations"
                        " (snapshot_id, from_entity, to_entity, kind, created_at)"
                        " VALUES (?, ?, ?, 'prereq', ?)",
                        (snapshot_id, mod_spec, dep_spec, now),
                    )
                except ValueError:
                    pass

            elif directive == "conflict":
                # field is the conflicting module; rest may have additional args
                conflict_spec = (rest.split()[0] if rest else field)
                try:
                    _validate_string("conflict_spec", conflict_spec)
                    now = _utc_now()
                    con.execute(
                        "INSERT INTO relations"
                        " (snapshot_id, from_entity, to_entity, kind, created_at)"
                        " VALUES (?, ?, ?, 'conflict', ?)",
                        (snapshot_id, mod_spec, conflict_spec, now),
                    )
                except ValueError:
                    pass

    # --- ELF analysis ---
    elf_paths = list(args.elf_path) if hasattr(args, "elf_path") and args.elf_path else []
    for elf_path in elf_paths:
        try:
            _validate_string("elf_path", elf_path)
        except ValueError:
            continue
        readelf_c = [readelf_cmd, "-d", elf_path]
        rc, stdout = _run_bounded(readelf_c, timeout, subprocess_env)
        cmd_sha = _sha256_cmd(readelf_c)
        now = _utc_now()
        con.execute(
            "INSERT INTO observations"
            " (snapshot_id, subject, kind, claim, evidence_level, outcome,"
            " provenance_sha256, command_exit_code, source_kind, created_at)"
            " VALUES (?, ?, 'elf_probe', 'readelf -d', 'detected', ?, ?, ?, 'readelf', ?)",
            (snapshot_id, elf_path,
             "success" if rc == 0 else "failure",
             cmd_sha, rc, now),
        )
        if rc == 0:
            for line in stdout.splitlines():
                mm = _ELF_TAG_RE.search(line)
                if not mm:
                    continue
                tag, val = mm.group(1), mm.group(2)
                try:
                    _validate_string("elf_val", val)
                except ValueError:
                    continue
                kind_map = {"NEEDED": "elf_needed", "RPATH": "elf_rpath", "RUNPATH": "elf_runpath"}
                rel_kind = kind_map.get(tag, "elf_tag")
                now = _utc_now()
                con.execute(
                    "INSERT INTO relations"
                    " (snapshot_id, from_entity, to_entity, kind, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (snapshot_id, elf_path, val, rel_kind, now),
                )

    # Update collection_status
    final_status = "incomplete" if collection_incomplete else "complete"
    con.execute(
        "UPDATE snapshots SET collection_status=?",
        (final_status,),
    )
    con.commit()
    con.close()
    return 0


# ---------------------------------------------------------------------------
# Command: search
# ---------------------------------------------------------------------------

def cmd_search(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    con = _open_ro(db_path)

    snap = con.execute("SELECT id, status, collection_status FROM snapshots").fetchone()
    snap_id = snap["id"] if snap else None
    collection_status = snap["collection_status"] if snap else "unknown"

    query = getattr(args, "query", "") or ""
    # Escape FTS5 special chars — use parameterized query
    rows = con.execute(
        "SELECT e.id, e.name, e.version, e.kind, e.evidence_level, e.source_kind"
        " FROM entity_fts f"
        " JOIN entities e ON e.id = f.rowid"
        " WHERE entity_fts MATCH ?"
        " ORDER BY e.name, e.version, e.kind",
        (query,),
    ).fetchall()

    results = []
    for row in rows:
        results.append({
            "name": row["name"],
            "version": row["version"],
            "kind": row["kind"],
            "evidence_level": row["evidence_level"],
            "source_kind": row["source_kind"],
            "source_db": str(db_path),
        })

    # Merge overlay if provided
    overlay_path = getattr(args, "overlay", None)
    if overlay_path:
        _merge_overlay_search(results, Path(overlay_path), query, str(db_path))

    output = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snap_id,
        "collection_status": collection_status,
        "query": query,
        "results": results,
    }
    print(json.dumps(output, sort_keys=True))
    con.close()
    return 0


def _merge_overlay_search(results: List, overlay_path: Path, query: str, site_db: str) -> None:
    """Append overlay observations matching query into results list (in-place)."""
    if not overlay_path.exists():
        return
    try:
        ov_con = sqlite3.connect(str(overlay_path))
        ov_con.row_factory = sqlite3.Row
        rows = ov_con.execute(
            "SELECT subject, kind, outcome, evidence_level, env_profile_id FROM observations"
            " WHERE subject LIKE ?"
            " ORDER BY subject, kind",
            (f"%{query}%",),
        ).fetchall()
        for row in rows:
            results.append({
                "name": row["subject"],
                "kind": row["kind"],
                "outcome": row["outcome"],
                "evidence_level": row["evidence_level"],
                "source_db": str(overlay_path),
            })
        ov_con.close()
    except Exception:
        pass  # Overlay errors are non-fatal


# ---------------------------------------------------------------------------
# Command: show
# ---------------------------------------------------------------------------

def cmd_show(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    name = args.name
    con = _open_ro(db_path)
    snap = con.execute("SELECT id FROM snapshots").fetchone()
    snap_id = snap["id"] if snap else None

    row = con.execute(
        "SELECT name, version, kind, evidence_level, source_kind, active, created_at"
        " FROM entities WHERE name=? OR (name || '/' || COALESCE(version,'')) = ?"
        " ORDER BY name, version LIMIT 1",
        (name, name),
    ).fetchone()

    if not row:
        con.close()
        output = {"error": f"not found: {name}", "snapshot_id": snap_id,
                  "schema_version": SCHEMA_VERSION}
        print(json.dumps(output, sort_keys=True))
        return 0

    entity = {
        "name": row["name"],
        "version": row["version"],
        "kind": row["kind"],
        "evidence_level": row["evidence_level"],
        "source_kind": row["source_kind"],
        "active": bool(row["active"]),
        "created_at": row["created_at"],
    }
    con.close()
    output = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snap_id,
        "entity": entity,
    }
    print(json.dumps(output, sort_keys=True))
    return 0


# ---------------------------------------------------------------------------
# Command: dependencies
# ---------------------------------------------------------------------------

def cmd_dependencies(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    name = args.name
    max_depth = getattr(args, "max_depth", 10) or 10
    limit = getattr(args, "limit", 100) or 100

    con = _open_ro(db_path)
    snap = con.execute("SELECT id FROM snapshots").fetchone()
    snap_id = snap["id"] if snap else None

    deps = _traverse_deps(con, name, max_depth, limit, forward=True)
    con.close()

    output = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snap_id,
        "name": name,
        "dependencies": deps,
    }
    print(json.dumps(output, sort_keys=True))
    return 0


def cmd_reverse_dependencies(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    name = args.name
    max_depth = getattr(args, "max_depth", 10) or 10
    limit = getattr(args, "limit", 100) or 100

    con = _open_ro(db_path)
    snap = con.execute("SELECT id FROM snapshots").fetchone()
    snap_id = snap["id"] if snap else None

    deps = _traverse_deps(con, name, max_depth, limit, forward=False)
    con.close()

    output = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snap_id,
        "name": name,
        "dependents": deps,
    }
    print(json.dumps(output, sort_keys=True))
    return 0


def _traverse_deps(
    con: sqlite3.Connection,
    root: str,
    max_depth: int,
    limit: int,
    forward: bool,
) -> List[Dict]:
    """BFS traversal of relations, cycle-safe, bounded by max_depth and limit."""
    visited = set()
    results = []
    queue = [(root, 0)]  # (entity_name, depth)

    while queue and len(results) < limit:
        current, depth = queue.pop(0)
        if current in visited or depth > max_depth:
            continue
        visited.add(current)

        if forward:
            rows = con.execute(
                "SELECT to_entity FROM relations WHERE from_entity=?"
                " AND kind IN ('prereq','load') ORDER BY to_entity",
                (current,),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT from_entity FROM relations WHERE to_entity=?"
                " AND kind IN ('prereq','load') ORDER BY from_entity",
                (current,),
            ).fetchall()

        for row in rows:
            neighbor = row[0]
            if neighbor not in visited and len(results) < limit:
                results.append({"name": neighbor, "depth": depth + 1})
                queue.append((neighbor, depth + 1))

    return results


# ---------------------------------------------------------------------------
# Command: status
# ---------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    con = _open_ro(db_path)
    snap = con.execute(
        "SELECT id, system, source_id, status, collection_status, created_at, finalized_at"
        " FROM snapshots"
    ).fetchone()
    if not snap:
        con.close()
        print(json.dumps({"error": "no snapshot found"}))
        return 1

    sv = con.execute(
        "SELECT value FROM metadata WHERE key='schema_version'"
    ).fetchone()
    tv = con.execute(
        "SELECT value FROM metadata WHERE key='tool_version'"
    ).fetchone()
    entity_count = con.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    relation_count = con.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
    obs_count = con.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    con.close()

    output = {
        "schema_version": int(sv[0]) if sv else None,
        "tool_version": tv[0] if tv else None,
        "snapshot_id": snap["id"],
        "system": snap["system"],
        "source_id": snap["source_id"],
        "status": snap["status"],
        "collection_status": snap["collection_status"],
        "created_at": snap["created_at"],
        "finalized_at": snap["finalized_at"],
        "entity_count": entity_count,
        "relation_count": relation_count,
        "observation_count": obs_count,
    }
    print(json.dumps(output, sort_keys=True))
    return 0


# ---------------------------------------------------------------------------
# Command: observe
# ---------------------------------------------------------------------------

_REQUIRED_OBSERVATION_FIELDS = {
    "subject", "kind", "claim", "evidence_level", "outcome",
    "evidence_path", "evidence_sha256", "command_exit_code", "env_profile_id",
}


def cmd_observe(args: argparse.Namespace) -> int:
    overlay_path = Path(args.overlay)
    site_path = Path(args.site)
    attempt_root = Path(args.attempt_root)

    try:
        payload = json.loads(args.input)
    except (json.JSONDecodeError, TypeError) as e:
        print(f"error: invalid JSON input: {e}", file=sys.stderr)
        return 1

    # Validate required fields
    missing = _REQUIRED_OBSERVATION_FIELDS - set(payload.keys())
    if missing:
        print(f"error: missing required fields: {sorted(missing)}", file=sys.stderr)
        return 1

    # Validate evidence_path
    ev_path = payload["evidence_path"]
    if not os.path.isabs(ev_path):
        print("error: evidence_path must be absolute", file=sys.stderr)
        return 1

    ev_path_obj = Path(ev_path).resolve()
    attempt_root_resolved = attempt_root.resolve()
    try:
        ev_path_obj.relative_to(attempt_root_resolved)
    except ValueError:
        print("error: evidence_path must be under attempt_root", file=sys.stderr)
        return 1

    # Secret-validate all string fields in payload
    for k, v in payload.items():
        if isinstance(v, str) and k != "evidence_sha256":
            try:
                _validate_string(k, v)
            except ValueError as e:
                print(f"error: {e}", file=sys.stderr)
                return 1

    # Create or open overlay atomically
    overlay_exists = overlay_path.exists()
    if not overlay_exists:
        # Initialize overlay schema
        tmp_fd, tmp_name = tempfile.mkstemp(
            suffix=".tmp.sqlite", dir=str(overlay_path.parent)
        )
        os.close(tmp_fd)
        tmp_p = Path(tmp_name)
        try:
            ov_con = sqlite3.connect(str(tmp_p))
            ov_con.executescript(_DDL)
            # Overlay gets its own snapshot referencing the site
            ov_snap_id = _new_id()
            now = _utc_now()
            ov_con.execute(
                "INSERT INTO snapshots"
                " (id, system, source_id, status, collection_status, created_at)"
                " VALUES (?, 'overlay', ?, 'open', 'pending', ?)",
                (ov_snap_id, str(site_path), now),
            )
            ov_con.execute(
                "INSERT INTO metadata (key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
            ov_con.execute(
                "INSERT INTO metadata (key, value) VALUES (?, ?)",
                ("tool_version", TOOL_VERSION),
            )
            ov_con.execute(
                "INSERT INTO metadata (key, value) VALUES (?, ?)",
                ("site_db", str(site_path)),
            )
            ov_con.commit()
            ov_con.close()
        except Exception as e:
            tmp_p.unlink(missing_ok=True)
            print(f"error: failed to initialize overlay: {e}", file=sys.stderr)
            return 1

        os.replace(tmp_name, str(overlay_path))
        os.chmod(str(overlay_path), 0o600)

    # Open overlay for writing
    ov_con = sqlite3.connect(str(overlay_path))
    ov_con.execute("PRAGMA foreign_keys=ON")
    ov_con.row_factory = sqlite3.Row

    try:
        snap = ov_con.execute("SELECT id FROM snapshots LIMIT 1").fetchone()
        snap_id = snap["id"] if snap else None
        now = _utc_now()

        ov_con.execute(
            "INSERT INTO observations"
            " (snapshot_id, subject, kind, claim, evidence_level, outcome,"
            " evidence_path, evidence_sha256, command_exit_code, env_profile_id,"
            " source_kind, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'observe', ?)",
            (
                snap_id,
                payload["subject"],
                payload["kind"],
                payload.get("claim"),
                payload["evidence_level"],
                payload["outcome"],
                ev_path,           # path reference only
                payload["evidence_sha256"],
                payload.get("command_exit_code"),
                payload.get("env_profile_id"),
                now,
            ),
        )
        ov_con.commit()
    except Exception as e:
        ov_con.rollback()
        ov_con.close()
        print(f"error: {e}", file=sys.stderr)
        return 1

    ov_con.close()
    os.chmod(str(overlay_path), 0o600)
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Red Shirt environment catalog CLI"
    )
    sub = parser.add_subparsers(dest="command")

    # init
    p_init = sub.add_parser("init")
    p_init.add_argument("--output", required=True)
    p_init.add_argument("--system", required=True)
    p_init.add_argument("--source-id", required=True)

    # finalize
    p_fin = sub.add_parser("finalize")
    p_fin.add_argument("--db", required=True)

    # verify
    p_ver = sub.add_parser("verify")
    p_ver.add_argument("--db", required=True)

    # collect
    p_col = sub.add_parser("collect")
    p_col.add_argument("--output", required=True)
    p_col.add_argument("--system", required=True)
    p_col.add_argument("--source-id", required=True)
    p_col.add_argument("--module", action="append", default=[])
    p_col.add_argument("--discover-modules", action="store_true", default=False)
    p_col.add_argument("--module-limit", type=int, default=None)
    p_col.add_argument("--command-timeout", type=int, default=30)
    p_col.add_argument("--elf-path", action="append", default=[])

    # search
    p_srch = sub.add_parser("search")
    p_srch.add_argument("--db", required=True)
    p_srch.add_argument("--query", required=True)
    p_srch.add_argument("--overlay", default=None)
    p_srch.add_argument("--limit", type=int, default=100)

    # show
    p_show = sub.add_parser("show")
    p_show.add_argument("--db", required=True)
    p_show.add_argument("--name", required=True)

    # dependencies
    p_dep = sub.add_parser("dependencies")
    p_dep.add_argument("--db", required=True)
    p_dep.add_argument("--name", required=True)
    p_dep.add_argument("--max-depth", type=int, default=10)
    p_dep.add_argument("--limit", type=int, default=100)

    # reverse-dependencies
    p_rdep = sub.add_parser("reverse-dependencies")
    p_rdep.add_argument("--db", required=True)
    p_rdep.add_argument("--name", required=True)
    p_rdep.add_argument("--max-depth", type=int, default=10)
    p_rdep.add_argument("--limit", type=int, default=100)

    # status
    p_stat = sub.add_parser("status")
    p_stat.add_argument("--db", required=True)

    # observe
    p_obs = sub.add_parser("observe")
    p_obs.add_argument("--overlay", required=True)
    p_obs.add_argument("--site", required=True)
    p_obs.add_argument("--input", required=True)
    p_obs.add_argument("--attempt-root", required=True)

    # insert-test-entity (test helper)
    p_ite = sub.add_parser("insert-test-entity")
    p_ite.add_argument("--db", required=True)
    p_ite.add_argument("--metadata", required=True)

    # insert-test-relation (test helper)
    p_itr = sub.add_parser("insert-test-relation")
    p_itr.add_argument("--db", required=True)
    p_itr.add_argument("--from", dest="from_entity", required=True)
    p_itr.add_argument("--to", dest="to_entity", required=True)
    p_itr.add_argument("--kind", required=True)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    dispatch = {
        "init": cmd_init,
        "finalize": cmd_finalize,
        "verify": cmd_verify,
        "collect": cmd_collect,
        "search": cmd_search,
        "show": cmd_show,
        "dependencies": cmd_dependencies,
        "reverse-dependencies": cmd_reverse_dependencies,
        "status": cmd_status,
        "observe": cmd_observe,
        "insert-test-entity": cmd_insert_test_entity,
        "insert-test-relation": cmd_insert_test_relation,
    }
    fn = dispatch.get(args.command)
    if not fn:
        print(f"error: unknown command: {args.command}", file=sys.stderr)
        return 1

    try:
        return fn(args)
    except Exception as e:
        print(f"error: unhandled exception: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
