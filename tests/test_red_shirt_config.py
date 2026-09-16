#!/usr/bin/env python3
"""Tests for scripts/red_shirt_config.py — fail-closed secret validation,
live-model selection, and Hermes config rendering.

Design: docs/superpowers/specs/2026-09-16-red-shirt-polaris-design.md
Plan:   docs/superpowers/plans/2026-09-16-red-shirt-polaris.md (Task 3)

All tests are offline/deterministic: secrets are temp files this process
creates and chmods; the ALCF catalog/jobs are supplied as fixture JSON files
via --catalog-fixture/--jobs-fixture (no network, no Globus token).

Run: pytest -q tests/test_red_shirt_config.py
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
RENDERER = SCRIPTS / "red_shirt_config.py"
TEMPLATE = REPO / "config" / "red-shirt-polaris" / "config.template.yaml"

sys.path.insert(0, str(SCRIPTS))


# ---------------------------------------------------------------------------
# Fixtures: real /models + /jobs shapes (trimmed), mirroring
# tests/test_populate_models.py's captured payloads.
# ---------------------------------------------------------------------------

CATALOG = [
    {"id": "live/model", "framework": "vllm", "max_model_len": 128000},
    {"id": "offline/model", "framework": "vllm", "max_model_len": 128000},
    {"id": "fallback/model", "framework": "vllm", "max_model_len": 131072},
    # below the 64k floor -> never eligible even if "running"
    {"id": "too-small/model", "framework": "vllm", "max_model_len": 16384},
]

JOBS_LIVE = {
    "running": [
        {"Models": "live/model,fallback/model,too-small/model",
         "Model Status": "running"},
    ],
    "queued": [],
}

JOBS_NONE_LIVE = {"running": [], "queued": []}


def _write(path: Path, content: str, mode: int = 0o600) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)
    return path


def _write_json(path: Path, data) -> Path:
    return _write(path, json.dumps(data), mode=0o644)


def _fake_token_helper(tmp_path: Path, token: str = "fake-inference-access-token") -> Path:
    helper = tmp_path / "fake_token_helper.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if __name__ == '__main__':\n"
        "    assert sys.argv[1:] == ['get_access_token'], sys.argv\n"
        f"    print({token!r})\n",
        encoding="utf-8",
    )
    helper.chmod(0o700)
    return helper


def _secrets(tmp_path: Path, *, inbound="a" * 20, outbound="b" * 20,
            headscale="c" * 10):
    inbound_path = _write(tmp_path / "inbound.token", inbound)
    outbound_path = _write(tmp_path / "outbound.token", outbound)
    headscale_path = _write(tmp_path / "headscale.key", headscale)
    return headscale_path, inbound_path, outbound_path


def run_cli(args: list) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(RENDERER)] + args,
        capture_output=True, text=True, timeout=60,
    )


def run_renderer(tmp_path: Path, *, catalog, jobs, preferred: str,
                 preferences: list, cluster: str = "sophia",
                 inbound=None, outbound=None) -> subprocess.CompletedProcess:
    _, inbound_path, outbound_path = _secrets(tmp_path)
    if inbound is not None:
        inbound_path = _write(tmp_path / "inbound_override.token", inbound)
    if outbound is not None:
        outbound_path = _write(tmp_path / "outbound_override.token", outbound)

    catalog_path = _write_json(tmp_path / "catalog.json", catalog)
    jobs_path = _write_json(tmp_path / "jobs.json", jobs)
    helper = _fake_token_helper(tmp_path)
    home = tmp_path / "home"

    args = [
        "render",
        "--home", str(home),
        "--template", str(TEMPLATE),
        "--token-helper", str(helper),
        "--catalog-fixture", str(catalog_path),
        "--jobs-fixture", str(jobs_path),
        "--inbound-a2a", str(inbound_path),
        "--outbound-a2a", str(outbound_path),
        "--preferred-model", preferred,
        "--cluster", cluster,
    ]
    for pref in preferences:
        args += ["--model-preference", pref]
    return run_cli(args)


# ---------------------------------------------------------------------------
# validate-secrets: metadata-only checks, never disclosing values
# ---------------------------------------------------------------------------

class TestValidateSecrets:
    def test_all_valid_secrets_pass(self, tmp_path):
        headscale, inbound, outbound = _secrets(tmp_path)
        result = run_cli([
            "validate-secrets",
            "--headscale-key", str(headscale),
            "--inbound-a2a", str(inbound),
            "--outbound-a2a", str(outbound),
        ])
        assert result.returncode == 0
        assert "OK" in result.stdout

    def test_missing_file_fails_closed(self, tmp_path):
        _, _, outbound = _secrets(tmp_path)
        headscale = _write(tmp_path / "headscale.key", "c" * 10)
        result = run_cli([
            "validate-secrets",
            "--headscale-key", str(headscale),
            "--inbound-a2a", str(tmp_path / "does-not-exist"),
            "--outbound-a2a", str(outbound),
        ])
        assert result.returncode != 0
        assert "does not exist" in result.stderr

    def test_wrong_mode_rejected(self, tmp_path):
        headscale, _, outbound = _secrets(tmp_path)
        bad = _write(tmp_path / "bad.token", "x" * 20, mode=0o644)
        result = run_cli([
            "validate-secrets",
            "--headscale-key", str(headscale),
            "--inbound-a2a", str(bad),
            "--outbound-a2a", str(outbound),
        ])
        assert result.returncode != 0
        assert "0600" in result.stderr

    def test_empty_file_rejected(self, tmp_path):
        headscale, _, outbound = _secrets(tmp_path)
        empty = _write(tmp_path / "empty.token", "")
        result = run_cli([
            "validate-secrets",
            "--headscale-key", str(headscale),
            "--inbound-a2a", str(empty),
            "--outbound-a2a", str(outbound),
        ])
        assert result.returncode != 0
        assert "empty" in result.stderr.lower()

    def test_directory_rejected(self, tmp_path):
        headscale, _, outbound = _secrets(tmp_path)
        d = tmp_path / "a_directory"
        d.mkdir()
        result = run_cli([
            "validate-secrets",
            "--headscale-key", str(headscale),
            "--inbound-a2a", str(d),
            "--outbound-a2a", str(outbound),
        ])
        assert result.returncode != 0
        assert "not a regular file" in result.stderr.lower()

    def test_short_a2a_token_rejected(self, tmp_path):
        headscale, _, outbound = _secrets(tmp_path)
        short = _write(tmp_path / "short.token", "tooshort")
        result = run_cli([
            "validate-secrets",
            "--headscale-key", str(headscale),
            "--inbound-a2a", str(short),
            "--outbound-a2a", str(outbound),
        ])
        assert result.returncode != 0
        assert "16" in result.stderr

    def test_a2a_token_exactly_16_chars_accepted(self, tmp_path):
        headscale, _, outbound = _secrets(tmp_path)
        exact = _write(tmp_path / "exact.token", "x" * 16)
        result = run_cli([
            "validate-secrets",
            "--headscale-key", str(headscale),
            "--inbound-a2a", str(exact),
            "--outbound-a2a", str(outbound),
        ])
        assert result.returncode == 0

    def test_headscale_key_has_no_minimum_length(self, tmp_path):
        # The headscale join key is not an A2A bearer token — no 16-char floor.
        _, inbound, outbound = _secrets(tmp_path)
        short = _write(tmp_path / "hs.key", "short")
        result = run_cli([
            "validate-secrets",
            "--headscale-key", str(short),
            "--inbound-a2a", str(inbound),
            "--outbound-a2a", str(outbound),
        ])
        assert result.returncode == 0

    def test_secret_value_never_appears_in_output(self, tmp_path):
        headscale, _, outbound = _secrets(tmp_path)
        secret_value = "unique-secret-value-abcdef123456"
        token_path = _write(tmp_path / "t.token", secret_value)
        result = run_cli([
            "validate-secrets",
            "--headscale-key", str(headscale),
            "--inbound-a2a", str(token_path),
            "--outbound-a2a", str(outbound),
        ])
        assert secret_value not in result.stdout
        assert secret_value not in result.stderr

    def test_symlink_rejected(self, tmp_path):
        headscale, _, outbound = _secrets(tmp_path)
        real = _write(tmp_path / "real.token", "x" * 20)
        link = tmp_path / "link.token"
        link.symlink_to(real)
        result = run_cli([
            "validate-secrets",
            "--headscale-key", str(headscale),
            "--inbound-a2a", str(link),
            "--outbound-a2a", str(outbound),
        ])
        assert result.returncode != 0

    def test_missing_all_arguments_rejected(self, tmp_path):
        """Regression for reviewer finding 1: bare `validate-secrets` with no
        credential arguments must fail closed (argparse usage error), never
        print `secrets: OK` / exit 0."""
        result = run_cli(["validate-secrets"])
        assert result.returncode != 0
        assert "OK" not in result.stdout
        assert "required" in result.stderr.lower()

    def test_missing_one_of_three_arguments_rejected(self, tmp_path):
        headscale, inbound, _ = _secrets(tmp_path)
        result = run_cli([
            "validate-secrets",
            "--headscale-key", str(headscale),
            "--inbound-a2a", str(inbound),
            # --outbound-a2a intentionally omitted
        ])
        assert result.returncode != 0
        assert "outbound-a2a" in result.stderr


# ---------------------------------------------------------------------------
# render: live-model selection + fail-closed exit 78
# ---------------------------------------------------------------------------

class TestRenderModelSelection:
    def test_selects_requested_model_when_live(self, tmp_path):
        result = run_renderer(
            tmp_path, catalog=CATALOG, jobs=JOBS_LIVE,
            preferred="live/model", preferences=["fallback/model"],
        )
        assert result.returncode == 0, result.stderr
        config = yaml.safe_load((tmp_path / "home" / "config.yaml").read_text())
        assert config["model"]["model"] == "live/model"

    def test_selects_live_model_in_preference_order(self, tmp_path):
        result = run_renderer(
            tmp_path, catalog=CATALOG, jobs=JOBS_LIVE,
            preferred="offline/model", preferences=["live/model", "fallback/model"],
        )
        assert result.returncode == 0, result.stderr
        config = yaml.safe_load((tmp_path / "home" / "config.yaml").read_text())
        assert config["model"]["model"] == "live/model"

    def test_falls_back_past_dead_preferences(self, tmp_path):
        result = run_renderer(
            tmp_path, catalog=CATALOG, jobs=JOBS_LIVE,
            preferred="offline/model",
            preferences=["not-in-catalog/model", "fallback/model"],
        )
        assert result.returncode == 0, result.stderr
        config = yaml.safe_load((tmp_path / "home" / "config.yaml").read_text())
        assert config["model"]["model"] == "fallback/model"

    def test_sub_floor_model_never_selected_even_if_running(self, tmp_path):
        result = run_renderer(
            tmp_path, catalog=CATALOG, jobs=JOBS_LIVE,
            preferred="too-small/model", preferences=["too-small/model"],
        )
        # too-small/model is running but below the 64k floor -> not eligible;
        # nothing else in the preference list -> fail closed.
        assert result.returncode == 78

    def test_refuses_when_no_usable_live_model(self, tmp_path):
        result = run_renderer(
            tmp_path, catalog=CATALOG, jobs=JOBS_NONE_LIVE,
            preferred="offline/model", preferences=["fallback/model"],
        )
        assert result.returncode == 78
        assert "no live >=64000-token chat model" in result.stderr.lower()

    def test_no_live_model_error_never_boots_a_config(self, tmp_path):
        result = run_renderer(
            tmp_path, catalog=CATALOG, jobs=JOBS_NONE_LIVE,
            preferred="offline/model", preferences=[],
        )
        assert result.returncode == 78
        assert not (tmp_path / "home" / "config.yaml").exists()


# ---------------------------------------------------------------------------
# render: rendered config content (provider URLs, context, caps, A2A)
# ---------------------------------------------------------------------------

class TestRenderedConfig:
    def _render_ok(self, tmp_path, **overrides):
        kwargs = dict(catalog=CATALOG, jobs=JOBS_LIVE,
                     preferred="live/model", preferences=[])
        kwargs.update(overrides)
        result = run_renderer(tmp_path, **kwargs)
        assert result.returncode == 0, result.stderr
        return yaml.safe_load((tmp_path / "home" / "config.yaml").read_text())

    def test_only_alcf_provider_urls_present(self, tmp_path):
        config = self._render_ok(tmp_path)
        assert config["model"]["base_url"].startswith(
            "https://inference-api.alcf.anl.gov/resource_server/"
        )
        for provider in config["custom_providers"]:
            assert provider["base_url"].startswith(
                "https://inference-api.alcf.anl.gov/resource_server/"
            )

    def test_correct_context_length_for_selected_model(self, tmp_path):
        config = self._render_ok(tmp_path)
        assert config["model"]["context_length"] == 128000
        provider = config["custom_providers"][0]
        assert provider["models"]["live/model"]["context_length"] == 128000

    def test_strip_tool_message_name_set(self, tmp_path):
        config = self._render_ok(tmp_path)
        assert config["model"]["strip_tool_message_name"] is True

    def test_reasoning_model_gets_reasoning_output_cap(self, tmp_path):
        import populate_models as pm
        catalog = CATALOG + [
            {"id": "openai/gpt-oss-mini", "framework": "vllm",
             "max_model_len": 128000, "reasoning_parser": "harmony"},
        ]
        jobs = {"running": [{"Models": "openai/gpt-oss-mini",
                             "Model Status": "running"}], "queued": []}
        config = self._render_ok(
            tmp_path, catalog=catalog, jobs=jobs,
            preferred="openai/gpt-oss-mini",
        )
        provider = config["custom_providers"][0]
        assert provider["name"].endswith("-reasoning")
        assert provider["max_tokens"] == pm.REASONING_MAX_TOKENS
        assert config["model"]["provider"] == f"custom:{provider['name']}"

    def test_non_reasoning_model_gets_baseline_output_cap(self, tmp_path):
        import populate_models as pm
        config = self._render_ok(tmp_path)
        provider = config["custom_providers"][0]
        assert not provider["name"].endswith("-reasoning")
        assert provider["max_tokens"] == pm.BASELINE_MAX_TOKENS

    def test_a2a_platform_enabled_on_loopback_port_9900(self, tmp_path):
        config = self._render_ok(tmp_path)
        a2a_platform = config["gateway"]["platforms"]["a2a"]
        assert a2a_platform["enabled"] is True
        assert a2a_platform["extra"]["port"] == 9900

    def test_a2a_trusted_peer_is_wesley_only(self, tmp_path):
        config = self._render_ok(tmp_path)
        assert config["a2a"]["trusted_peers"] == ["wesley"]

    def test_outbound_wesley_agent_registered(self, tmp_path):
        config = self._render_ok(tmp_path)
        wesley = config["a2a_agents"]["wesley"]
        assert wesley["url"]
        assert wesley["auth"]["type"] == "bearer"

    def test_config_contains_no_literal_a2a_token_values(self, tmp_path):
        inbound_secret = "inbound-literal-value-1234567890"
        outbound_secret = "outbound-literal-value-1234567890"
        config = self._render_ok(tmp_path, inbound=inbound_secret,
                                 outbound=outbound_secret)
        raw = (tmp_path / "home" / "config.yaml").read_text()
        assert inbound_secret not in raw
        assert outbound_secret not in raw

    def test_no_unresolved_env_placeholders_except_the_two_secret_refs(self, tmp_path):
        """Requirement 8 (amended by manager decision reconciling reviewer
        finding 2 with the pinned Hermes v2026.9.14 A2A contract, which has
        no outbound token_env/token_file resolver — plugins/platforms/a2a/
        tools.py::_auth_header only reads auth.token after config.yaml's own
        ${VAR} expansion): generated config.yaml MUST contain no unresolved
        ${...} placeholders EXCEPT exactly the two credential references
        ${ALCF_ACCESS_TOKEN} and ${A2A_OUTBOUND_WESLEY_TOKEN}. Those two are
        the only supported way to keep token literals out of config.yaml
        while still authenticating through the existing Hermes A2A client;
        every other templated field must resolve to a literal value. This is
        an exact allowlist, not `<= 2` or `any two` — an extra or different
        placeholder anywhere in the file fails this test."""
        import re
        self._render_ok(tmp_path)
        raw = (tmp_path / "home" / "config.yaml").read_text()
        refs = set(re.findall(r"\$\{([^}]+)\}", raw))
        assert refs == {"ALCF_ACCESS_TOKEN", "A2A_OUTBOUND_WESLEY_TOKEN"}, (
            f"unexpected unresolved ${{...}} placeholders: {refs}"
        )


# ---------------------------------------------------------------------------
# render: secret env handling
# ---------------------------------------------------------------------------

class TestSecretEnv:
    def _render_ok(self, tmp_path, **overrides):
        kwargs = dict(catalog=CATALOG, jobs=JOBS_LIVE,
                     preferred="live/model", preferences=[])
        kwargs.update(overrides)
        result = run_renderer(tmp_path, **kwargs)
        assert result.returncode == 0, result.stderr
        return result

    def test_env_file_written_mode_0600(self, tmp_path):
        self._render_ok(tmp_path)
        env_path = tmp_path / "home" / ".env"
        assert env_path.exists()
        mode = stat.S_IMODE(env_path.stat().st_mode)
        assert mode == 0o600

    def test_env_file_contains_access_token_and_a2a_tokens(self, tmp_path):
        self._render_ok(tmp_path, inbound="in" * 10, outbound="out" * 10)
        env_text = (tmp_path / "home" / ".env").read_text()
        assert "ALCF_ACCESS_TOKEN=" in env_text
        assert "in" * 10 in env_text
        assert "out" * 10 in env_text

    def test_no_secret_values_on_stdout_or_stderr(self, tmp_path):
        inbound_secret = "stdout-guard-inbound-1234567890"
        outbound_secret = "stdout-guard-outbound-1234567890"
        result = run_renderer(
            tmp_path, catalog=CATALOG, jobs=JOBS_LIVE,
            preferred="live/model", preferences=[],
            inbound=inbound_secret, outbound=outbound_secret,
        )
        assert result.returncode == 0, result.stderr
        assert inbound_secret not in result.stdout
        assert outbound_secret not in result.stdout
        assert "fake-inference-access-token" not in result.stdout
        assert inbound_secret not in result.stderr
        assert outbound_secret not in result.stderr

    def test_stdout_reports_only_model_and_provider_names(self, tmp_path):
        result = run_renderer(
            tmp_path, catalog=CATALOG, jobs=JOBS_LIVE,
            preferred="live/model", preferences=[],
        )
        assert "live/model" in result.stdout
        assert "config.yaml" in result.stdout or "home" in result.stdout


# ---------------------------------------------------------------------------
# render: atomicity
# ---------------------------------------------------------------------------

class TestAtomicity:
    def test_rerender_replaces_config_atomically_no_partial_file(self, tmp_path):
        result1 = run_renderer(
            tmp_path, catalog=CATALOG, jobs=JOBS_LIVE,
            preferred="live/model", preferences=[],
        )
        assert result1.returncode == 0, result1.stderr
        config_path = tmp_path / "home" / "config.yaml"
        first = config_path.read_text()

        result2 = run_renderer(
            tmp_path, catalog=CATALOG, jobs=JOBS_LIVE,
            preferred="fallback/model", preferences=[],
        )
        assert result2.returncode == 0, result2.stderr
        second = config_path.read_text()
        assert first != second
        # No leftover temp files in the home directory.
        leftovers = [p for p in (tmp_path / "home").iterdir()
                    if p.name.startswith(".config.yaml.")]
        assert leftovers == []


# ---------------------------------------------------------------------------
# Direct unit tests against the importable functions (not just the CLI)
# ---------------------------------------------------------------------------

class TestUnitLevel:
    def test_validate_secrets_raises_with_no_disclosure(self, tmp_path):
        import red_shirt_config as rsc
        bad = _write(tmp_path / "bad.token", "x" * 20, mode=0o644)
        with pytest.raises(rsc.SecretValidationError) as excinfo:
            rsc.validate_secrets(inbound_a2a=str(bad))
        assert "x" * 20 not in str(excinfo.value)

    def test_select_live_model_prefers_requested(self):
        import red_shirt_config as rsc
        model_id, ctx, is_reasoning = rsc.select_live_model(
            CATALOG, JOBS_LIVE, preferred="live/model",
            preferences=["fallback/model"], cluster="sophia",
        )
        assert model_id == "live/model"
        assert ctx == 128000
        assert is_reasoning is False

    def test_select_live_model_raises_when_nothing_live(self):
        import red_shirt_config as rsc
        with pytest.raises(rsc.NoLiveModelError):
            rsc.select_live_model(
                CATALOG, JOBS_NONE_LIVE, preferred="offline/model",
                preferences=[], cluster="sophia",
            )


# ---------------------------------------------------------------------------
# Real Hermes load_config() compatibility (best-effort; skipped if the
# Hermes venv/config loader is not reachable from this checkout).
# ---------------------------------------------------------------------------

def _find_hermes_agent_root() -> "Path | None":
    """Locate a hermes-agent checkout with a usable venv, if one exists.

    Best-effort discovery: looks under $HOME/.hermes/hermes-agent (the
    layout used by this box's operator profile). Returns None rather than
    raising, so this test degrades to a skip on a machine without it.
    """
    candidate = Path.home() / ".hermes" / "hermes-agent"
    venv_python = candidate / "venv" / "bin" / "python3"
    if candidate.is_dir() and venv_python.exists():
        return candidate
    return None


class TestRealHermesConfigLoader:
    def test_rendered_config_loads_with_real_hermes_load_config(self, tmp_path):
        hermes_root = _find_hermes_agent_root()
        if hermes_root is None:
            pytest.skip("no local hermes-agent checkout with a venv found")

        result = run_renderer(
            tmp_path, catalog=CATALOG, jobs=JOBS_LIVE,
            preferred="live/model", preferences=[],
        )
        assert result.returncode == 0, result.stderr
        home = tmp_path / "home"

        venv_python = hermes_root / "venv" / "bin" / "python3"
        code = (
            "import sys, os, json\n"
            f"sys.path.insert(0, {str(hermes_root)!r})\n"
            f"os.environ['HERMES_HOME'] = {str(home)!r}\n"
            "from hermes_cli.env_loader import load_hermes_dotenv\n"
            f"load_hermes_dotenv(hermes_home={str(home)!r})\n"
            "from hermes_cli.config import load_config\n"
            "cfg = load_config()\n"
            "print(json.dumps({\n"
            "    'a2a_enabled': cfg.get('gateway', {}).get('platforms', {}).get('a2a', {}).get('enabled'),\n"
            "    'has_custom_provider': bool(cfg.get('custom_providers')),\n"
            "    'api_key': cfg.get('model', {}).get('api_key'),\n"
            "    'wesley_token': cfg.get('a2a_agents', {}).get('wesley', {}).get('auth', {}).get('token'),\n"
            "}))\n"
        )
        proc = subprocess.run(
            [str(venv_python), "-c", code],
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        assert payload["a2a_enabled"] is True
        assert payload["has_custom_provider"] is True
        # The literal api_key placeholder must have been expanded by
        # Hermes's own loader against the .env this renderer wrote — i.e.
        # it must no longer be the unresolved ${ALCF_ACCESS_TOKEN} string.
        assert payload["api_key"] != "${ALCF_ACCESS_TOKEN}"
        assert payload["api_key"] == "fake-inference-access-token"
        # Same requirement for the outbound Wesley A2A bearer token: the
        # ${A2A_OUTBOUND_WESLEY_TOKEN} raw-config reference (the second and
        # only other allowed unresolved placeholder) must resolve through
        # real load_config() + .env expansion before plugins/platforms/a2a/
        # tools.py::_auth_header ever sees it, since that function reads
        # only the already-expanded auth.token literal.
        assert payload["wesley_token"] != "${A2A_OUTBOUND_WESLEY_TOKEN}"
        assert payload["wesley_token"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
