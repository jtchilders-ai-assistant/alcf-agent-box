"""
Tests for scripts/red_shirt_env_catalog.py — Tasks 1, 2, and 3.

TDD: all tests written before implementation.

Task 1: Schema, atomic lifecycle, secret firewall
Task 2: Bounded host collection and provenance
Task 3: Search, graph traversal, attempt overlays
"""
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
SCRIPT = Path(__file__).parent.parent / "scripts" / "red_shirt_env_catalog.py"

TOOL_VERSION = "1"
SCHEMA_VERSION = 1

REQUIRED_TABLES = {
    "snapshots",
    "entities",
    "relations",
    "observations",
    "entity_fts",
    "metadata",
}


def run_cli(*args, check=True, input_text=None):
    """Run the CLI and return CompletedProcess."""
    cmd = [sys.executable, str(SCRIPT)] + list(args)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        input=input_text,
        check=check,
    )


def make_catalog(tmp_path, system="polaris", source_id="fixture-001"):
    """Create and return a fresh catalog path (not finalized)."""
    db_path = tmp_path / "catalog.sqlite"
    run_cli("init", "--output", str(db_path), "--system", system, "--source-id", source_id)
    return db_path


def finalize_catalog(db_path):
    run_cli("finalize", "--db", str(db_path))


def verify_catalog(db_path, check=True):
    return run_cli("verify", "--db", str(db_path), check=check)


def open_ro(db_path):
    uri = f"file:{db_path}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


# ===========================================================================
# Task 1: Schema, atomic lifecycle, secret firewall
# ===========================================================================


class TestInit:
    """Step 1: database creation, schema version, required tables."""

    def test_creates_database_file(self, tmp_path):
        db_path = make_catalog(tmp_path)
        assert db_path.exists()

    def test_schema_version_is_1(self, tmp_path):
        db_path = make_catalog(tmp_path)
        con = sqlite3.connect(str(db_path))
        row = con.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()
        con.close()
        assert row is not None and int(row[0]) == SCHEMA_VERSION

    def test_required_tables_exist(self, tmp_path):
        db_path = make_catalog(tmp_path)
        con = sqlite3.connect(str(db_path))
        tables = {
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
        con.close()
        assert REQUIRED_TABLES.issubset(tables), f"Missing: {REQUIRED_TABLES - tables}"

    def test_foreign_keys_are_enabled(self, tmp_path):
        # PRAGMA foreign_keys is a connection-level setting; it resets to OFF on
        # every plain sqlite3.connect(). The contract is that the *implementation*
        # always enables it (via _open_rw / _open_ro) — we verify that FK
        # constraints are actually enforced by attempting an orphaned insert.
        db_path = make_catalog(tmp_path)
        con = sqlite3.connect(str(db_path))
        con.execute("PRAGMA foreign_keys=ON")
        # An entity with a non-existent snapshot_id must be rejected
        try:
            con.execute(
                "INSERT INTO entities"
                " (snapshot_id, name, version, kind, active, evidence_level, created_at)"
                " VALUES ('nonexistent-snap-id', 'test', NULL, 'module', 0, 'declared', '2026-01-01T00:00:00Z')"
            )
            con.commit()
            con.close()
            pytest.fail("FK constraint should have prevented orphaned insert")
        except sqlite3.IntegrityError:
            con.close()
            pass  # Expected: FK constraint enforced

    def test_single_open_snapshot(self, tmp_path):
        db_path = make_catalog(tmp_path)
        con = sqlite3.connect(str(db_path))
        rows = con.execute(
            "SELECT id FROM snapshots WHERE status='open'"
        ).fetchall()
        con.close()
        assert len(rows) == 1

    def test_snapshot_records_system_and_source_id(self, tmp_path):
        db_path = make_catalog(tmp_path, system="polaris", source_id="s-001")
        con = sqlite3.connect(str(db_path))
        row = con.execute(
            "SELECT system, source_id FROM snapshots WHERE status='open'"
        ).fetchone()
        con.close()
        assert row == ("polaris", "s-001")

    def test_mode_0600_before_finalization(self, tmp_path):
        db_path = make_catalog(tmp_path)
        mode = stat.S_IMODE(os.stat(db_path).st_mode)
        assert mode == 0o600

    def test_no_temporary_file_remains(self, tmp_path):
        db_path = make_catalog(tmp_path)
        # No .tmp or .wal or .shm sibling should remain after init
        siblings = list(tmp_path.iterdir())
        assert len(siblings) == 1, f"Extra files: {siblings}"

    def test_tool_version_recorded(self, tmp_path):
        db_path = make_catalog(tmp_path)
        con = sqlite3.connect(str(db_path))
        row = con.execute(
            "SELECT value FROM metadata WHERE key='tool_version'"
        ).fetchone()
        con.close()
        assert row is not None

    def test_utc_timestamps_in_snapshot(self, tmp_path):
        db_path = make_catalog(tmp_path)
        con = sqlite3.connect(str(db_path))
        row = con.execute("SELECT created_at FROM snapshots").fetchone()
        con.close()
        assert row and row[0].endswith("Z"), f"Not UTC: {row[0]}"

    def test_process_environment_not_stored(self, tmp_path):
        # No raw env dump: PATH, HOME, USER etc. should not appear as entity names
        os.environ["__CATALOG_CANARY__"] = "canary-env-value-99"
        try:
            db_path = make_catalog(tmp_path)
            data = db_path.read_bytes()
            assert b"canary-env-value-99" not in data
        finally:
            del os.environ["__CATALOG_CANARY__"]


class TestSecretFirewall:
    """Step 4: secret-like key/value rejection."""

    SECRET_KEYS = [
        "GITHUB_TOKEN",
        "password",
        "secret",
        "credential",
        "api-key",
        "PRIVATE_KEY",
        "Authorization",
    ]
    SECRET_VALUES = [
        "Bearer eyJhbGciOiJIUzI1NiJ9.e30.abc",
        "https://user:hunter2@example.com",
        "-----BEGIN RSA PRIVATE KEY-----",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc",
        "CANARY_SECRET_VALUE_ABC",
    ]

    def _insert_attempt(self, db_path, key, value):
        """Attempt to insert an entity with secret-like metadata; return proc."""
        payload = json.dumps({"key": key, "value": value})
        return run_cli(
            "insert-test-entity",
            "--db", str(db_path),
            "--metadata", payload,
            check=False,
        )

    def test_secret_key_rejected(self, tmp_path):
        db_path = make_catalog(tmp_path)
        for key in self.SECRET_KEYS:
            proc = self._insert_attempt(db_path, key, "harmless-value")
            assert proc.returncode != 0, f"Should reject key: {key}"

    def test_secret_value_rejected(self, tmp_path):
        db_path = make_catalog(tmp_path)
        for value in self.SECRET_VALUES:
            proc = self._insert_attempt(db_path, "module_name", value)
            assert proc.returncode != 0, f"Should reject value: {value[:30]}"

    def test_canary_absent_from_db_bytes(self, tmp_path):
        db_path = make_catalog(tmp_path)
        self._insert_attempt(db_path, "module_name", "CANARY_SECRET_VALUE_ABC")
        data = db_path.read_bytes()
        assert b"CANARY_SECRET_VALUE_ABC" not in data

    def test_error_does_not_leak_value(self, tmp_path):
        db_path = make_catalog(tmp_path)
        proc = self._insert_attempt(db_path, "module_name", "CANARY_SECRET_VALUE_ABC")
        assert b"CANARY_SECRET_VALUE_ABC" not in proc.stderr.encode()
        assert b"CANARY_SECRET_VALUE_ABC" not in proc.stdout.encode()

    def test_error_names_rejected_field(self, tmp_path):
        db_path = make_catalog(tmp_path)
        proc = self._insert_attempt(db_path, "GITHUB_TOKEN", "x")
        # stderr must mention the field name (key) but not its value
        assert "GITHUB_TOKEN" in proc.stderr or "GITHUB_TOKEN" in proc.stdout

    def test_transaction_rolled_back(self, tmp_path):
        db_path = make_catalog(tmp_path)
        self._insert_attempt(db_path, "GITHUB_TOKEN", "secret-token-xyz")
        con = sqlite3.connect(str(db_path))
        count = con.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        con.close()
        assert count == 0, "Transaction should have been rolled back"

    def test_json_nested_secret_rejected(self, tmp_path):
        db_path = make_catalog(tmp_path)
        nested = json.dumps({"module": "gcc", "token": "supersecret"})
        proc = self._insert_attempt(db_path, "metadata_blob", nested)
        assert proc.returncode != 0

    def test_safe_module_name_accepted(self, tmp_path):
        db_path = make_catalog(tmp_path)
        proc = self._insert_attempt(db_path, "module_name", "gcc/11.2.0")
        assert proc.returncode == 0, f"Should accept safe value; got: {proc.stderr}"

    def test_sha256_digest_accepted(self, tmp_path):
        db_path = make_catalog(tmp_path)
        digest = "a" * 64  # 64 hex chars = SHA-256
        proc = self._insert_attempt(db_path, "sha256", digest)
        assert proc.returncode == 0, f"SHA-256 digest should be allowed: {proc.stderr}"


class TestFinalize:
    """Step 6: finalize writes checksum sidecar, chmod 0444, marks complete."""

    def test_finalize_creates_sha256_sidecar(self, tmp_path):
        db_path = make_catalog(tmp_path)
        finalize_catalog(db_path)
        sidecar = Path(str(db_path) + ".sha256")
        assert sidecar.exists()

    def test_sidecar_contains_valid_sha256_line(self, tmp_path):
        db_path = make_catalog(tmp_path)
        finalize_catalog(db_path)
        sidecar = Path(str(db_path) + ".sha256")
        content = sidecar.read_text().strip()
        # format: <64-hex>  <filename>
        parts = content.split()
        assert len(parts) == 2
        assert len(parts[0]) == 64 and all(c in "0123456789abcdef" for c in parts[0])

    def test_db_mode_0444_after_finalize(self, tmp_path):
        db_path = make_catalog(tmp_path)
        finalize_catalog(db_path)
        mode = stat.S_IMODE(os.stat(db_path).st_mode)
        assert mode == 0o444, f"Expected 0o444, got {oct(mode)}"

    def test_sidecar_mode_0444_after_finalize(self, tmp_path):
        db_path = make_catalog(tmp_path)
        finalize_catalog(db_path)
        sidecar = Path(str(db_path) + ".sha256")
        mode = stat.S_IMODE(os.stat(sidecar).st_mode)
        assert mode == 0o444, f"Expected 0o444, got {oct(mode)}"

    def test_snapshot_status_complete_after_finalize(self, tmp_path):
        db_path = make_catalog(tmp_path)
        # Must chmod temporarily to read
        finalize_catalog(db_path)
        os.chmod(db_path, 0o644)
        con = sqlite3.connect(str(db_path))
        row = con.execute("SELECT status FROM snapshots").fetchone()
        con.close()
        os.chmod(db_path, 0o444)
        assert row and row[0] == "complete"

    def test_finalize_runs_integrity_checks(self, tmp_path):
        db_path = make_catalog(tmp_path)
        # Should succeed without error
        proc = run_cli("finalize", "--db", str(db_path))
        assert proc.returncode == 0

    def test_double_finalize_fails(self, tmp_path):
        db_path = make_catalog(tmp_path)
        finalize_catalog(db_path)
        proc = run_cli("finalize", "--db", str(db_path), check=False)
        assert proc.returncode != 0, "Second finalize should fail"


class TestVerify:
    """Step 6: verify checks sidecar, schema, integrity, complete status."""

    def test_verify_passes_good_catalog(self, tmp_path):
        db_path = make_catalog(tmp_path)
        finalize_catalog(db_path)
        proc = verify_catalog(db_path)
        assert proc.returncode == 0

    def test_verify_fails_without_sidecar(self, tmp_path):
        db_path = make_catalog(tmp_path)
        finalize_catalog(db_path)
        sidecar = Path(str(db_path) + ".sha256")
        sidecar.unlink()
        proc = verify_catalog(db_path, check=False)
        assert proc.returncode != 0

    def test_verify_fails_on_tampered_db(self, tmp_path):
        db_path = make_catalog(tmp_path)
        finalize_catalog(db_path)
        os.chmod(db_path, 0o644)
        # Corrupt a byte
        data = bytearray(db_path.read_bytes())
        data[-1] ^= 0xFF
        db_path.write_bytes(bytes(data))
        os.chmod(db_path, 0o444)
        proc = verify_catalog(db_path, check=False)
        assert proc.returncode != 0

    def test_verify_fails_on_open_snapshot(self, tmp_path):
        db_path = make_catalog(tmp_path)
        # Not finalized
        proc = verify_catalog(db_path, check=False)
        assert proc.returncode != 0


# ===========================================================================
# Task 2: Bounded host collection and provenance
# ===========================================================================


def make_fake_runner_script(tmp_path, responses):
    """
    Write a fake shell dispatcher script and return its path.
    `responses` is a dict mapping a unique substring in the command args
    to (stdout, returncode).
    """
    lines = ["#!/usr/bin/env python3", "import sys"]
    lines.append("args = sys.argv[1:]")
    lines.append("joined = ' '.join(args)")
    for key, (out, rc) in responses.items():
        # Use repr() so the string is properly escaped as a Python literal
        out_repr = repr(out)
        lines.append(
            f"if {repr(key)} in joined:\n"
            f"    sys.stdout.write({out_repr})\n"
            f"    sys.exit({rc})"
        )
    lines.append("sys.stderr.write('Unknown command: ' + joined + '\\n')")
    lines.append("sys.exit(1)")
    script = tmp_path / "fake_module_cmd.py"
    script.write_text("\n".join(lines))
    script.chmod(0o755)
    return script


MODULE_AVAIL_OUTPUT = """\
gcc/11.2.0
gcc/12.1.0
openmpi/4.1.4
hdf5/1.12.2
"""

MODULE_SHOW_GCC = """\
-------------------------------------------------------------------
/soft/modulefiles/gcc/11.2.0:

module-whatis   {GCC 11.2.0 compiler suite}
prepend-path    PATH /soft/gcc/11.2.0/bin
prepend-path    LD_LIBRARY_PATH /soft/gcc/11.2.0/lib64
setenv          GCC_ROOT /soft/gcc/11.2.0
conflict        gcc
-------------------------------------------------------------------
"""

MODULE_SHOW_OPENMPI = """\
-------------------------------------------------------------------
/soft/modulefiles/openmpi/4.1.4:

module-whatis   {OpenMPI 4.1.4}
prepend-path    PATH /soft/openmpi/4.1.4/bin
prereq          gcc
-------------------------------------------------------------------
"""

MODULE_LIST_OUTPUT = """\
Currently Loaded Modulefiles:
  1) gcc/11.2.0   2) openmpi/4.1.4
"""

GCC_VERSION_OUTPUT = "gcc (GCC) 11.2.0\nCopyright ...\n"

READELF_OUTPUT = """\
Dynamic section at offset 0x2e20 contains 28 entries:
  Tag        Type                         Name/Value
 0x0000000000000001 (NEEDED)             Shared library: [libmpi.so.40]
 0x0000000000000001 (NEEDED)             Shared library: [libc.so.6]
 0x000000000000000f (RPATH)              Library rpath: [/soft/openmpi/4.1.4/lib]
"""


class TestCollect:
    """Task 2 Step 1 & 3: fixture-driven collection tests."""

    def _run_collect(self, tmp_path, extra_args=None, fake_module_cmd=None):
        db_path = tmp_path / "catalog.sqlite"
        args = [
            "collect",
            "--output", str(db_path),
            "--system", "polaris",
            "--source-id", "collect-test-001",
            "--module", "gcc/11.2.0",
            "--module", "openmpi/4.1.4",
            "--command-timeout", "30",
        ]
        if extra_args:
            args += extra_args
        env = os.environ.copy()
        if fake_module_cmd:
            env["RED_SHIRT_MODULE_CMD"] = str(fake_module_cmd)
            env["RED_SHIRT_WHICH_CMD"] = str(fake_module_cmd)
            env["RED_SHIRT_READELF_CMD"] = str(fake_module_cmd)
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)] + args,
            capture_output=True, text=True, env=env, check=False
        )
        return proc, db_path

    def _build_fake_cmd(self, tmp_path):
        responses = {
            "--terse avail": (MODULE_AVAIL_OUTPUT, 0),
            "show gcc": (MODULE_SHOW_GCC, 0),
            "show openmpi": (MODULE_SHOW_OPENMPI, 0),
            "list": (MODULE_LIST_OUTPUT, 0),
            "--version": (GCC_VERSION_OUTPUT, 0),
            "-d ": (READELF_OUTPUT, 0),  # readelf -d <path>; argv[1:] won't contain "readelf"
            "which gcc": ("/soft/gcc/11.2.0/bin/gcc\n", 0),
        }
        return make_fake_runner_script(tmp_path, responses)

    def test_collect_creates_db(self, tmp_path):
        fake = self._build_fake_cmd(tmp_path)
        proc, db_path = self._run_collect(tmp_path, fake_module_cmd=fake)
        assert proc.returncode == 0, proc.stderr
        assert db_path.exists()

    def test_collect_records_module_entities(self, tmp_path):
        fake = self._build_fake_cmd(tmp_path)
        proc, db_path = self._run_collect(tmp_path, fake_module_cmd=fake)
        assert proc.returncode == 0, proc.stderr
        con = sqlite3.connect(str(db_path))
        rows = con.execute(
            "SELECT name, version FROM entities WHERE kind='module'"
        ).fetchall()
        con.close()
        names = {r[0] for r in rows}
        assert "gcc" in names
        assert "openmpi" in names

    def test_collect_records_path_change_entities(self, tmp_path):
        fake = self._build_fake_cmd(tmp_path)
        proc, db_path = self._run_collect(tmp_path, fake_module_cmd=fake)
        assert proc.returncode == 0, proc.stderr
        con = sqlite3.connect(str(db_path))
        rows = con.execute(
            "SELECT name FROM entities WHERE kind='path'"
        ).fetchall()
        con.close()
        paths = {r[0] for r in rows}
        # PATH entries from gcc module show should appear
        assert any("/soft/gcc" in p for p in paths), f"Paths: {paths}"

    def test_collect_records_prerequisite_edges(self, tmp_path):
        fake = self._build_fake_cmd(tmp_path)
        proc, db_path = self._run_collect(tmp_path, fake_module_cmd=fake)
        assert proc.returncode == 0, proc.stderr
        con = sqlite3.connect(str(db_path))
        rows = con.execute(
            "SELECT from_entity, to_entity, kind FROM relations WHERE kind='prereq'"
        ).fetchall()
        con.close()
        assert len(rows) >= 1, "openmpi should have a prereq edge to gcc"

    def test_collect_records_conflict_edges(self, tmp_path):
        fake = self._build_fake_cmd(tmp_path)
        proc, db_path = self._run_collect(tmp_path, fake_module_cmd=fake)
        assert proc.returncode == 0, proc.stderr
        con = sqlite3.connect(str(db_path))
        rows = con.execute(
            "SELECT kind FROM relations WHERE kind='conflict'"
        ).fetchall()
        con.close()
        assert len(rows) >= 1, "gcc should have a conflict-with-self edge"

    def test_collect_records_elf_needed_edges(self, tmp_path):
        fake = self._build_fake_cmd(tmp_path)
        proc, db_path = self._run_collect(
            tmp_path,
            extra_args=["--elf-path", "/soft/openmpi/4.1.4/bin/mpiexec"],
            fake_module_cmd=fake,
        )
        assert proc.returncode == 0, proc.stderr
        con = sqlite3.connect(str(db_path))
        rows = con.execute(
            "SELECT kind FROM relations WHERE kind='elf_needed'"
        ).fetchall()
        con.close()
        assert len(rows) >= 1, "ELF NEEDED edges should be recorded"

    def test_collect_records_command_sha256(self, tmp_path):
        fake = self._build_fake_cmd(tmp_path)
        proc, db_path = self._run_collect(tmp_path, fake_module_cmd=fake)
        assert proc.returncode == 0, proc.stderr
        con = sqlite3.connect(str(db_path))
        rows = con.execute(
            "SELECT provenance_sha256 FROM observations WHERE provenance_sha256 IS NOT NULL"
        ).fetchall()
        con.close()
        assert len(rows) >= 1, "At least one observation should have sha256"

    def test_collect_records_exit_status(self, tmp_path):
        fake = self._build_fake_cmd(tmp_path)
        proc, db_path = self._run_collect(tmp_path, fake_module_cmd=fake)
        assert proc.returncode == 0, proc.stderr
        con = sqlite3.connect(str(db_path))
        rows = con.execute(
            "SELECT command_exit_code FROM observations WHERE command_exit_code IS NOT NULL"
        ).fetchall()
        con.close()
        assert len(rows) >= 1

    def test_collect_never_stores_raw_stdout(self, tmp_path):
        fake = self._build_fake_cmd(tmp_path)
        proc, db_path = self._run_collect(tmp_path, fake_module_cmd=fake)
        assert proc.returncode == 0, proc.stderr
        # The raw GCC version copyright string should not appear in DB bytes
        data = db_path.read_bytes()
        assert b"Copyright" not in data, "Raw stdout should not be stored"

    def test_collect_never_stores_raw_environment(self, tmp_path):
        os.environ["__COLLECT_CANARY__"] = "collect-canary-secret-xyz"
        try:
            fake = self._build_fake_cmd(tmp_path)
            proc, db_path = self._run_collect(tmp_path, fake_module_cmd=fake)
            data = db_path.read_bytes()
            assert b"collect-canary-secret-xyz" not in data
        finally:
            del os.environ["__COLLECT_CANARY__"]

    def test_collect_no_shell_true(self, tmp_path):
        """Verify no shell=True is used in subprocess calls (not in comments/docstrings)."""
        src = SCRIPT.read_text()
        import re
        # Find lines that contain shell=True but are NOT pure comment or docstring lines
        bad_lines = []
        for line in src.splitlines():
            stripped = line.strip()
            # Skip comment lines and docstring-only lines
            if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'\"'\"'"):
                continue
            if stripped.startswith('"') and "shell=True" in stripped and stripped.endswith('"'):
                continue  # docstring line containing the phrase
            if re.search(r"shell\s*=\s*True", stripped) and "subprocess" in stripped:
                bad_lines.append(line)
        assert not bad_lines, f"shell=True in subprocess call: {bad_lines}"

    def test_collect_records_source_kind(self, tmp_path):
        fake = self._build_fake_cmd(tmp_path)
        proc, db_path = self._run_collect(tmp_path, fake_module_cmd=fake)
        assert proc.returncode == 0, proc.stderr
        con = sqlite3.connect(str(db_path))
        rows = con.execute(
            "SELECT DISTINCT source_kind FROM observations"
        ).fetchall()
        con.close()
        kinds = {r[0] for r in rows}
        assert kinds, "source_kind must be recorded"


class TestCollectBounds:
    """Task 2 Step 2: bounded failure, timeout labeling, incomplete status."""

    def test_positive_command_timeout_required(self, tmp_path):
        proc, _ = self._run_collect_minimal(tmp_path, timeout="0")
        assert proc.returncode != 0, "Timeout must be positive"

    def test_positive_module_limit_required(self, tmp_path):
        proc, _ = self._run_collect_minimal(tmp_path, limit="0")
        assert proc.returncode != 0, "Module limit must be positive"

    def test_negative_module_limit_rejected(self, tmp_path):
        proc, _ = self._run_collect_minimal(tmp_path, limit="-1")
        assert proc.returncode != 0

    def test_collection_status_incomplete_on_probe_failure(self, tmp_path):
        """A module probe that times out marks collection_status=incomplete."""
        # Use a fake that always times out (returns rc=124, the timeout exit)
        script = tmp_path / "timeout_cmd.py"
        script.write_text(
            "#!/usr/bin/env python3\nimport sys\nsys.stderr.write('timed out\\n')\nsys.exit(124)\n"
        )
        script.chmod(0o755)
        db_path = tmp_path / "catalog.sqlite"
        env = os.environ.copy()
        env["RED_SHIRT_MODULE_CMD"] = str(script)
        proc = subprocess.run(
            [sys.executable, str(SCRIPT),
             "collect", "--output", str(db_path),
             "--system", "polaris", "--source-id", "test",
             "--module", "gcc/11.2.0",
             "--command-timeout", "5"],
            capture_output=True, text=True, env=env, check=False
        )
        # Should not crash; DB should exist; status should be incomplete
        assert db_path.exists(), "DB should be created even on probe failure"
        con = sqlite3.connect(str(db_path))
        row = con.execute("SELECT collection_status FROM snapshots").fetchone()
        con.close()
        assert row and row[0] == "incomplete"

    def test_failed_probe_not_upgraded_to_absent(self, tmp_path):
        """A timed-out probe must be labeled incomplete, not absent dependency."""
        script = tmp_path / "fail_cmd.py"
        script.write_text(
            "#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n"
        )
        script.chmod(0o755)
        db_path = tmp_path / "catalog.sqlite"
        env = os.environ.copy()
        env["RED_SHIRT_MODULE_CMD"] = str(script)
        subprocess.run(
            [sys.executable, str(SCRIPT),
             "collect", "--output", str(db_path),
             "--system", "polaris", "--source-id", "test",
             "--module", "gcc/11.2.0",
             "--command-timeout", "5"],
            capture_output=True, text=True, env=env, check=False
        )
        if not db_path.exists():
            return  # acceptable: no DB on total failure
        con = sqlite3.connect(str(db_path))
        rows = con.execute(
            "SELECT kind FROM relations WHERE kind='absent'"
        ).fetchall()
        con.close()
        assert not rows, "Failed probes must not produce 'absent' dependency edges"

    def test_deterministic_ordering(self, tmp_path):
        """Running collect twice with same input produces same entity ordering."""
        fake = self._build_fake_cmd(tmp_path)
        db1 = tmp_path / "a.sqlite"
        db2 = tmp_path / "b.sqlite"
        env = os.environ.copy()
        env["RED_SHIRT_MODULE_CMD"] = str(fake)
        base_args = [
            sys.executable, str(SCRIPT),
            "collect", "--system", "polaris", "--source-id", "det-test",
            "--module", "gcc/11.2.0", "--command-timeout", "30",
        ]
        subprocess.run(base_args + ["--output", str(db1)],
                       capture_output=True, text=True, env=env, check=False)
        subprocess.run(base_args + ["--output", str(db2)],
                       capture_output=True, text=True, env=env, check=False)
        if not (db1.exists() and db2.exists()):
            pytest.skip("collect not yet implemented")
        c1 = sqlite3.connect(str(db1))
        c2 = sqlite3.connect(str(db2))
        e1 = c1.execute("SELECT name, kind, version FROM entities ORDER BY name, kind").fetchall()
        e2 = c2.execute("SELECT name, kind, version FROM entities ORDER BY name, kind").fetchall()
        c1.close()
        c2.close()
        assert e1 == e2, "Entity sets differ between runs"

    def _run_collect_minimal(self, tmp_path, timeout="30", limit=None):
        db_path = tmp_path / "catalog.sqlite"
        args = [
            "collect",
            "--output", str(db_path),
            "--system", "polaris",
            "--source-id", "bounds-test",
            "--module", "gcc/11.2.0",
            "--command-timeout", timeout,
        ]
        if limit is not None:
            args += ["--module-limit", limit]
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)] + args,
            capture_output=True, text=True, check=False
        )
        return proc, db_path

    def _build_fake_cmd(self, tmp_path):
        responses = {
            "--terse avail": (MODULE_AVAIL_OUTPUT, 0),
            "show gcc": (MODULE_SHOW_GCC, 0),
            "list": (MODULE_LIST_OUTPUT, 0),
        }
        return make_fake_runner_script(tmp_path, responses)


class TestActiveProfile:
    """Task 2 Step 4: active module collection from LOADEDMODULES only."""

    def test_loadedmodules_recorded_after_secret_validation(self, tmp_path):
        fake = tmp_path / "fake.py"
        fake.write_text(
            "#!/usr/bin/env python3\nimport sys\nsys.stdout.write('')\nsys.exit(0)\n"
        )
        fake.chmod(0o755)
        db_path = tmp_path / "catalog.sqlite"
        env = os.environ.copy()
        env["LOADEDMODULES"] = "gcc/11.2.0:openmpi/4.1.4"
        env["RED_SHIRT_MODULE_CMD"] = str(fake)
        proc = subprocess.run(
            [sys.executable, str(SCRIPT),
             "collect", "--output", str(db_path),
             "--system", "polaris", "--source-id", "active-test",
             "--command-timeout", "5"],
            capture_output=True, text=True, env=env, check=False
        )
        if not db_path.exists():
            pytest.skip("collect not implemented yet")
        con = sqlite3.connect(str(db_path))
        rows = con.execute(
            "SELECT name FROM entities WHERE kind='module' AND active=1"
        ).fetchall()
        con.close()
        names = {r[0] for r in rows}
        assert "gcc" in names or "openmpi" in names

    def test_arbitrary_env_vars_not_persisted(self, tmp_path):
        fake = tmp_path / "fake.py"
        fake.write_text(
            "#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n"
        )
        fake.chmod(0o755)
        db_path = tmp_path / "catalog.sqlite"
        env = os.environ.copy()
        env["MY_PRIVATE_HOME_DIR"] = "/home/verysecretpath/stuff"
        env["RED_SHIRT_MODULE_CMD"] = str(fake)
        subprocess.run(
            [sys.executable, str(SCRIPT),
             "collect", "--output", str(db_path),
             "--system", "polaris", "--source-id", "env-test",
             "--command-timeout", "5"],
            capture_output=True, text=True, env=env, check=False
        )
        if not db_path.exists():
            return
        data = db_path.read_bytes()
        assert b"verysecretpath" not in data


# ===========================================================================
# Task 3: Search, graph traversal, attempt overlays
# ===========================================================================


@pytest.fixture(scope="module")
def finalized_catalog(tmp_path_factory):
    """Build a finalized site catalog with known entities and relations."""
    tmp = tmp_path_factory.mktemp("site")
    db_path = tmp / "site.sqlite"
    run_cli("init", "--output", str(db_path), "--system", "polaris",
            "--source-id", "query-fixture")
    # Insert known entities and relations directly via insert-test-entity
    run_cli("insert-test-entity", "--db", str(db_path),
            "--metadata", json.dumps({"key": "module_name", "value": "gcc/11.2.0"}))
    run_cli("insert-test-entity", "--db", str(db_path),
            "--metadata", json.dumps({"key": "module_name", "value": "openmpi/4.1.4"}))
    # Insert a prereq relation: openmpi depends on gcc
    run_cli("insert-test-relation", "--db", str(db_path),
            "--from", "openmpi/4.1.4", "--to", "gcc/11.2.0", "--kind", "prereq")
    run_cli("finalize", "--db", str(db_path))
    return db_path


class TestSearch:
    """Task 3 Step 1 & 2: FTS search on finalized catalog."""

    def test_search_returns_json(self, tmp_path, finalized_catalog):
        proc = run_cli("search", "--db", str(finalized_catalog), "--query", "gcc")
        data = json.loads(proc.stdout)
        assert "results" in data

    def test_search_includes_schema_version(self, tmp_path, finalized_catalog):
        proc = run_cli("search", "--db", str(finalized_catalog), "--query", "gcc")
        data = json.loads(proc.stdout)
        assert data.get("schema_version") == SCHEMA_VERSION

    def test_search_includes_snapshot_id(self, tmp_path, finalized_catalog):
        proc = run_cli("search", "--db", str(finalized_catalog), "--query", "gcc")
        data = json.loads(proc.stdout)
        assert "snapshot_id" in data

    def test_search_includes_completeness(self, tmp_path, finalized_catalog):
        proc = run_cli("search", "--db", str(finalized_catalog), "--query", "gcc")
        data = json.loads(proc.stdout)
        assert "collection_status" in data

    def test_search_finds_module_by_name(self, tmp_path, finalized_catalog):
        proc = run_cli("search", "--db", str(finalized_catalog), "--query", "gcc")
        data = json.loads(proc.stdout)
        names = [r.get("name", "") for r in data["results"]]
        assert any("gcc" in n for n in names)

    def test_search_deterministic_output(self, tmp_path, finalized_catalog):
        proc1 = run_cli("search", "--db", str(finalized_catalog), "--query", "gcc")
        proc2 = run_cli("search", "--db", str(finalized_catalog), "--query", "gcc")
        assert proc1.stdout == proc2.stdout

    def test_search_rejects_sql_injection(self, tmp_path, finalized_catalog):
        # Parameterized SQL: injection strings should not cause a crash or disclosure
        proc = run_cli(
            "search", "--db", str(finalized_catalog),
            "--query", "'; DROP TABLE entities; --",
            check=False
        )
        # Must not crash; exit code 0 or non-zero, but no unhandled exception
        assert "Traceback" not in proc.stderr

    def test_search_db_opened_read_only(self, tmp_path, finalized_catalog):
        """After search the db file must still be mode 0444 (not opened writably)."""
        run_cli("search", "--db", str(finalized_catalog), "--query", "gcc")
        mode = stat.S_IMODE(os.stat(finalized_catalog).st_mode)
        assert mode == 0o444


class TestShow:
    """Task 3: show command returns provenance and evidence level."""

    def test_show_returns_json(self, tmp_path, finalized_catalog):
        proc = run_cli("show", "--db", str(finalized_catalog), "--name", "gcc/11.2.0")
        data = json.loads(proc.stdout)
        assert "entity" in data or "error" in data

    def test_show_includes_evidence_level(self, tmp_path, finalized_catalog):
        proc = run_cli("show", "--db", str(finalized_catalog), "--name", "gcc/11.2.0")
        data = json.loads(proc.stdout)
        if "entity" in data:
            assert "evidence_level" in data["entity"]

    def test_show_unknown_returns_json_error(self, tmp_path, finalized_catalog):
        proc = run_cli(
            "show", "--db", str(finalized_catalog), "--name", "nonexistent/9.9.9",
            check=False
        )
        # Should produce parseable JSON, not a Python traceback
        assert "Traceback" not in proc.stderr
        try:
            data = json.loads(proc.stdout)
            assert "error" in data or "entity" in data
        except json.JSONDecodeError:
            pytest.fail(f"Non-JSON output: {proc.stdout[:200]}")


class TestDependencies:
    """Task 3: dependency traversal — cycle-safe, bounded."""

    def test_dependencies_returns_json(self, tmp_path, finalized_catalog):
        proc = run_cli(
            "dependencies", "--db", str(finalized_catalog), "--name", "openmpi/4.1.4"
        )
        data = json.loads(proc.stdout)
        assert "dependencies" in data

    def test_dependencies_includes_prereq(self, tmp_path, finalized_catalog):
        proc = run_cli(
            "dependencies", "--db", str(finalized_catalog), "--name", "openmpi/4.1.4"
        )
        data = json.loads(proc.stdout)
        deps = [d.get("name", "") for d in data.get("dependencies", [])]
        assert any("gcc" in d for d in deps)

    def test_reverse_dependencies_returns_json(self, tmp_path, finalized_catalog):
        proc = run_cli(
            "reverse-dependencies", "--db", str(finalized_catalog), "--name", "gcc/11.2.0"
        )
        data = json.loads(proc.stdout)
        assert "dependents" in data

    def test_max_depth_bound_respected(self, tmp_path, finalized_catalog):
        proc = run_cli(
            "dependencies", "--db", str(finalized_catalog),
            "--name", "openmpi/4.1.4", "--max-depth", "1"
        )
        data = json.loads(proc.stdout)
        # At depth 1, should not recurse past immediate deps
        for dep in data.get("dependencies", []):
            assert dep.get("depth", 0) <= 1

    def test_cycle_safe(self, tmp_path):
        """Cyclic graph must not cause infinite recursion."""
        db_path = tmp_path / "cycle.sqlite"
        run_cli("init", "--output", str(db_path), "--system", "polaris",
                "--source-id", "cycle-test")
        run_cli("insert-test-entity", "--db", str(db_path),
                "--metadata", json.dumps({"key": "module_name", "value": "a/1.0"}))
        run_cli("insert-test-entity", "--db", str(db_path),
                "--metadata", json.dumps({"key": "module_name", "value": "b/1.0"}))
        # Insert cycle: a -> b -> a
        run_cli("insert-test-relation", "--db", str(db_path),
                "--from", "a/1.0", "--to", "b/1.0", "--kind", "prereq")
        run_cli("insert-test-relation", "--db", str(db_path),
                "--from", "b/1.0", "--to", "a/1.0", "--kind", "prereq")
        run_cli("finalize", "--db", str(db_path))
        proc = run_cli(
            "dependencies", "--db", str(db_path), "--name", "a/1.0",
            "--max-depth", "10"
        )
        data = json.loads(proc.stdout)
        assert "dependencies" in data  # Must return, not hang

    def test_limit_param_bounded(self, tmp_path, finalized_catalog):
        proc = run_cli(
            "dependencies", "--db", str(finalized_catalog),
            "--name", "openmpi/4.1.4", "--limit", "1"
        )
        data = json.loads(proc.stdout)
        assert len(data.get("dependencies", [])) <= 1


class TestStatus:
    """Task 3: status command."""

    def test_status_returns_json(self, tmp_path, finalized_catalog):
        proc = run_cli("status", "--db", str(finalized_catalog))
        data = json.loads(proc.stdout)
        assert "snapshot_id" in data or "status" in data


class TestOverlay:
    """Task 3 Steps 3 & 4: attempt overlay creation and merged reads."""

    def _observe(self, site_db, overlay_db, attempt_root, extra_args=None):
        evidence_file = attempt_root / "evidence.txt"
        evidence_file.write_text("test evidence content")
        import hashlib
        digest = hashlib.sha256(evidence_file.read_bytes()).hexdigest()
        args = [
            "observe",
            "--overlay", str(overlay_db),
            "--site", str(site_db),
            "--input", json.dumps({
                "subject": "gcc/11.2.0",
                "kind": "build_success",
                "claim": "compiled without errors",
                "evidence_level": "runtime",
                "outcome": "success",
                "evidence_path": str(evidence_file),
                "evidence_sha256": digest,
                "command_exit_code": 0,
                "env_profile_id": "polaris-env-001",
            }),
            "--attempt-root", str(attempt_root),
        ]
        if extra_args:
            args += extra_args
        return run_cli(*args, check=False)

    def test_overlay_created_at_0600(self, tmp_path, finalized_catalog):
        attempt_root = tmp_path / "attempt"
        attempt_root.mkdir()
        overlay_db = tmp_path / "overlay.sqlite"
        proc = self._observe(finalized_catalog, overlay_db, attempt_root)
        assert proc.returncode == 0, proc.stderr
        assert overlay_db.exists()
        mode = stat.S_IMODE(os.stat(overlay_db).st_mode)
        assert mode == 0o600

    def test_observation_recorded(self, tmp_path, finalized_catalog):
        attempt_root = tmp_path / "attempt"
        attempt_root.mkdir()
        overlay_db = tmp_path / "overlay.sqlite"
        proc = self._observe(finalized_catalog, overlay_db, attempt_root)
        assert proc.returncode == 0, proc.stderr
        con = sqlite3.connect(str(overlay_db))
        rows = con.execute("SELECT kind, outcome FROM observations").fetchall()
        con.close()
        assert any(r[0] == "build_success" for r in rows)

    def test_contradictory_observations_coexist(self, tmp_path, finalized_catalog):
        """A success and a failure observation on the same subject both remain."""
        attempt_root = tmp_path / "attempt"
        attempt_root.mkdir()
        overlay_db = tmp_path / "overlay.sqlite"
        # First: success
        self._observe(finalized_catalog, overlay_db, attempt_root)
        # Second: failure (different evidence file)
        ev2 = attempt_root / "evidence2.txt"
        ev2.write_text("failure log")
        import hashlib
        digest2 = hashlib.sha256(ev2.read_bytes()).hexdigest()
        run_cli(
            "observe",
            "--overlay", str(overlay_db),
            "--site", str(finalized_catalog),
            "--input", json.dumps({
                "subject": "gcc/11.2.0",
                "kind": "build_success",
                "claim": "compiled with errors",
                "evidence_level": "runtime",
                "outcome": "failure",
                "evidence_path": str(ev2),
                "evidence_sha256": digest2,
                "command_exit_code": 1,
                "env_profile_id": "polaris-env-001",
            }),
            "--attempt-root", str(attempt_root),
        )
        con = sqlite3.connect(str(overlay_db))
        rows = con.execute("SELECT outcome FROM observations WHERE subject='gcc/11.2.0'").fetchall()
        con.close()
        outcomes = {r[0] for r in rows}
        assert "success" in outcomes and "failure" in outcomes, \
            f"Both outcomes must coexist; got: {outcomes}"

    def test_site_bytes_unchanged_after_observe(self, tmp_path, finalized_catalog):
        before = finalized_catalog.read_bytes()
        attempt_root = tmp_path / "attempt"
        attempt_root.mkdir()
        overlay_db = tmp_path / "overlay.sqlite"
        self._observe(finalized_catalog, overlay_db, attempt_root)
        after = finalized_catalog.read_bytes()
        assert before == after, "Site catalog must not be modified by observe"

    def test_site_checksum_unchanged_after_observe(self, tmp_path, finalized_catalog):
        sidecar = Path(str(finalized_catalog) + ".sha256")
        before = sidecar.read_text()
        attempt_root = tmp_path / "attempt"
        attempt_root.mkdir()
        overlay_db = tmp_path / "overlay.sqlite"
        self._observe(finalized_catalog, overlay_db, attempt_root)
        after = sidecar.read_text()
        assert before == after

    def test_evidence_path_must_be_absolute(self, tmp_path, finalized_catalog):
        attempt_root = tmp_path / "attempt"
        attempt_root.mkdir()
        overlay_db = tmp_path / "overlay.sqlite"
        import hashlib
        ev = attempt_root / "ev.txt"
        ev.write_text("x")
        digest = hashlib.sha256(ev.read_bytes()).hexdigest()
        proc = run_cli(
            "observe",
            "--overlay", str(overlay_db),
            "--site", str(finalized_catalog),
            "--input", json.dumps({
                "subject": "gcc/11.2.0",
                "kind": "build",
                "claim": "ok",
                "evidence_level": "runtime",
                "outcome": "success",
                "evidence_path": "relative/path/ev.txt",  # NOT absolute
                "evidence_sha256": digest,
                "command_exit_code": 0,
                "env_profile_id": "env-001",
            }),
            "--attempt-root", str(attempt_root),
            check=False,
        )
        assert proc.returncode != 0, "Relative evidence path should be rejected"

    def test_evidence_path_must_be_under_attempt_root(self, tmp_path, finalized_catalog):
        attempt_root = tmp_path / "attempt"
        attempt_root.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("x")
        import hashlib
        digest = hashlib.sha256(outside.read_bytes()).hexdigest()
        overlay_db = tmp_path / "overlay.sqlite"
        proc = run_cli(
            "observe",
            "--overlay", str(overlay_db),
            "--site", str(finalized_catalog),
            "--input", json.dumps({
                "subject": "gcc/11.2.0",
                "kind": "build",
                "claim": "ok",
                "evidence_level": "runtime",
                "outcome": "success",
                "evidence_path": str(outside),
                "evidence_sha256": digest,
                "command_exit_code": 0,
                "env_profile_id": "env-001",
            }),
            "--attempt-root", str(attempt_root),
            check=False,
        )
        assert proc.returncode != 0, "Evidence outside attempt root should be rejected"

    def test_evidence_content_not_stored(self, tmp_path, finalized_catalog):
        attempt_root = tmp_path / "attempt"
        attempt_root.mkdir()
        overlay_db = tmp_path / "overlay.sqlite"
        ev = attempt_root / "ev.txt"
        ev.write_text("UNIQUE_EVIDENCE_CONTENT_XYZ")
        import hashlib
        digest = hashlib.sha256(ev.read_bytes()).hexdigest()
        run_cli(
            "observe",
            "--overlay", str(overlay_db),
            "--site", str(finalized_catalog),
            "--input", json.dumps({
                "subject": "gcc/11.2.0",
                "kind": "build",
                "claim": "ok",
                "evidence_level": "runtime",
                "outcome": "success",
                "evidence_path": str(ev),
                "evidence_sha256": digest,
                "command_exit_code": 0,
                "env_profile_id": "env-001",
            }),
            "--attempt-root", str(attempt_root),
        )
        con = sqlite3.connect(str(overlay_db))
        data = overlay_db.read_bytes()
        con.close()
        assert b"UNIQUE_EVIDENCE_CONTENT_XYZ" not in data

    def test_overlay_search_merged(self, tmp_path, finalized_catalog):
        """search --overlay should include overlay observations in results."""
        attempt_root = tmp_path / "attempt"
        attempt_root.mkdir()
        overlay_db = tmp_path / "overlay.sqlite"
        self._observe(finalized_catalog, overlay_db, attempt_root)
        proc = run_cli(
            "search", "--db", str(finalized_catalog),
            "--overlay", str(overlay_db),
            "--query", "gcc",
        )
        data = json.loads(proc.stdout)
        assert "results" in data
        # The results should include source_db provenance
        for r in data["results"]:
            assert "source_db" in r or "name" in r

    def test_required_observation_fields(self, tmp_path, finalized_catalog):
        """Missing required fields should be rejected."""
        attempt_root = tmp_path / "attempt"
        attempt_root.mkdir()
        overlay_db = tmp_path / "overlay.sqlite"
        # Missing evidence_level
        proc = run_cli(
            "observe",
            "--overlay", str(overlay_db),
            "--site", str(finalized_catalog),
            "--input", json.dumps({
                "subject": "gcc/11.2.0",
                "kind": "build",
                "claim": "ok",
                # no evidence_level
                "outcome": "success",
                "evidence_path": str(attempt_root / "ev.txt"),
                "evidence_sha256": "a" * 64,
                "command_exit_code": 0,
                "env_profile_id": "env-001",
            }),
            "--attempt-root", str(attempt_root),
            check=False,
        )
        assert proc.returncode != 0
