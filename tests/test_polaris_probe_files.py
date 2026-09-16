#!/usr/bin/env python3
"""
Test suite for the Polaris egress probe PBS script.

Tests verify ONLY the static structure of deploy/polaris/probe-egress.pbs —
no job is submitted and no network calls are made.

Run:
    pytest tests/test_polaris_probe_files.py -v
"""
from __future__ import annotations

import os
import re
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PBS_SCRIPT = os.path.join(REPO, "deploy", "polaris", "probe-egress.pbs")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_script() -> str:
    """Read the PBS script, failing with a clear message if absent."""
    assert os.path.isfile(PBS_SCRIPT), (
        f"PBS script not found: {PBS_SCRIPT}\n"
        "Create deploy/polaris/probe-egress.pbs to pass these tests."
    )
    with open(PBS_SCRIPT) as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Existence & syntax
# ---------------------------------------------------------------------------

class TestScriptExists:
    def test_file_exists(self):
        """deploy/polaris/probe-egress.pbs must exist."""
        assert os.path.isfile(PBS_SCRIPT), (
            f"Missing: {PBS_SCRIPT}"
        )

    def test_bash_syntax(self):
        """bash -n must report zero errors."""
        assert os.path.isfile(PBS_SCRIPT), f"Missing: {PBS_SCRIPT}"
        result = subprocess.run(
            ["bash", "-n", PBS_SCRIPT],
            capture_output=True, text=True
        )
        assert result.returncode == 0, (
            f"bash -n reported syntax errors:\n{result.stderr}"
        )


# ---------------------------------------------------------------------------
# PBS directives
# ---------------------------------------------------------------------------

class TestPBSDirectives:
    """#PBS header lines must meet Polaris requirements."""

    def test_select_system_polaris(self):
        """select=1:system=polaris is required."""
        content = _read_script()
        assert re.search(
            r"^#PBS\s+-l\s+select=1:system=polaris",
            content, re.MULTILINE
        ), "#PBS -l select=1:system=polaris not found"

    def test_queue_debug(self):
        """Queue must be 'debug'."""
        content = _read_script()
        assert re.search(
            r"^#PBS\s+-q\s+debug",
            content, re.MULTILINE
        ), "#PBS -q debug not found"

    def test_walltime_max_ten_minutes(self):
        """Walltime must be <= 00:10:00."""
        content = _read_script()
        m = re.search(
            r"^#PBS\s+-l\s+walltime=(\d+):(\d+):(\d+)",
            content, re.MULTILINE
        )
        assert m, "#PBS -l walltime= directive not found"
        hours, minutes, seconds = int(m.group(1)), int(m.group(2)), int(m.group(3))
        total_seconds = hours * 3600 + minutes * 60 + seconds
        assert total_seconds <= 600, (
            f"Walltime {m.group(0)} exceeds 10-minute limit"
        )

    def test_filesystems_home(self):
        """filesystems=home must appear in a PBS resource line."""
        content = _read_script()
        assert re.search(
            r"^#PBS\s+-l\s+.*filesystems=home",
            content, re.MULTILINE
        ), "#PBS -l filesystems=home not found"

    def test_no_pbs_project_directive(self):
        """#PBS -A must NOT appear — Polaris rejects literal shell variable
        expansions in PBS directives.  The project must be passed via
        'qsub -A "$ALCF_PROJECT"' on the command line instead."""
        content = _read_script()
        m = re.search(r"^#PBS\s+-A\b", content, re.MULTILINE)
        assert not m, (
            "Found '#PBS -A' directive — Polaris PBS does not expand shell "
            "variables in directives, so '#PBS -A ${ALCF_PROJECT}' submits "
            "a job under the literal string '${ALCF_PROJECT}' and is "
            "rejected.  Remove the directive and require the caller to pass "
            "'-A \"$ALCF_PROJECT\"' to qsub on the command line."
        )

    def test_usage_comment_shows_qsub_dash_A(self):
        """Usage comment must instruct callers to supply -A on the qsub line."""
        content = _read_script()
        # Accept any line like: qsub -A "$ALCF_PROJECT" ... deploy/...probe.pbs
        assert re.search(
            r"qsub\s+-A\s+[\"']?\$(?:\{ALCF_PROJECT\}|ALCF_PROJECT)[\"']?",
            content
        ), (
            "Usage comment must show 'qsub -A \"$ALCF_PROJECT\" "
            "deploy/polaris/probe-egress.pbs' (not a #PBS -A directive)"
        )


# ---------------------------------------------------------------------------
# Proxy environment
# ---------------------------------------------------------------------------

class TestProxyExports:
    """Both HTTP_PROXY and HTTPS_PROXY (upper and lower) must be exported."""

    PROXY_URL = "http://proxy.alcf.anl.gov:3128"

    def _check_proxy_var(self, varname: str):
        content = _read_script()
        pattern = rf"export\s+{re.escape(varname)}={re.escape(self.PROXY_URL)}"
        assert re.search(pattern, content), (
            f"'export {varname}={self.PROXY_URL}' not found in script"
        )

    def test_export_HTTP_PROXY_upper(self):
        self._check_proxy_var("HTTP_PROXY")

    def test_export_http_proxy_lower(self):
        self._check_proxy_var("http_proxy")

    def test_export_HTTPS_PROXY_upper(self):
        self._check_proxy_var("HTTPS_PROXY")

    def test_export_https_proxy_lower(self):
        self._check_proxy_var("https_proxy")


# ---------------------------------------------------------------------------
# curl calls
# ---------------------------------------------------------------------------

class TestCurlCalls:
    """Validate the curl invocations in the script body."""

    def test_direct_curl_negative_control(self):
        """A direct (no-proxy) curl call must exist as a negative control."""
        content = _read_script()
        # curl invocations may span multiple lines via backslash continuation;
        # search the whole script for --noproxy anywhere near a curl call.
        assert re.search(r"--noproxy", content), (
            "No direct curl (--noproxy) negative control found"
        )

    def test_proxied_curl_icanhazip(self):
        """A proxied curl to icanhazip.com must appear to expose public IP."""
        content = _read_script()
        # URL may be on a continuation line; search whole content.
        assert re.search(r"https://icanhazip\.com", content), (
            "curl to https://icanhazip.com not found"
        )

    def test_no_insecure_flag(self):
        """Neither -k nor --insecure may appear in the script."""
        content = _read_script()
        assert not re.search(r"\bcurl\b[^\n]*(?:-k\b|--insecure)", content), (
            "curl with -k / --insecure found — TLS must be verified"
        )

    def test_proxied_tls_sslip_host(self):
        """A proxied TLS request to the sslip.io host must appear."""
        content = _read_script()
        # URL may be on a continuation line; search whole content.
        assert re.search(r"https://143\.198\.112\.69\.sslip\.io", content), (
            "curl to https://143.198.112.69.sslip.io not found"
        )

    def test_captures_http_status(self):
        """Script must capture HTTP status code (write-out or -o /dev/null -w)."""
        content = _read_script()
        assert re.search(
            r"--write-out|%\{http_code\}|-w\b",
            content
        ), "HTTP status capture (--write-out / %{http_code}) not found"

    def test_connect_to_numeric_ip(self):
        """TLS curl must use --connect-to to route via numeric IP, avoiding
        the Polaris DNS sinkhole of 143.198.112.69.sslip.io."""
        content = _read_script()
        # Expect: --connect-to 143.198.112.69.sslip.io:443:143.198.112.69:443
        assert re.search(
            r"--connect-to\s+143\.198\.112\.69\.sslip\.io:443:143\.198\.112\.69:443",
            content
        ), (
            "--connect-to 143.198.112.69.sslip.io:443:143.198.112.69:443 not found. "
            "Polaris sinkholes the sslip.io hostname to a honeypot; use --connect-to "
            "to force the numeric IP while keeping TLS SNI/hostname as sslip.io."
        )

    def test_cacert_flag_present(self):
        """TLS curl must use --cacert to supply the private Caddy CA certificate
        (not -k / --insecure).  The flag may reference a variable."""
        content = _read_script()
        assert re.search(r"--cacert\s+\S+", content), (
            "--cacert <ca-file> not found in TLS curl invocation. "
            "The Caddy CA is private and not trusted on Polaris; "
            "supply it via --cacert instead of -k."
        )

    def test_tls_curl_rc_captured_and_printed(self):
        """Script must capture the curl exit code separately as tls_curl_rc
        and print it.  HTTP status alone is insufficient to detect TLS failures
        (curl exits non-zero on handshake error even when HTTP 000 is emitted)."""
        content = _read_script()
        assert re.search(r"tls_curl_rc", content), (
            "tls_curl_rc not found. Capture curl's exit code separately "
            "(e.g. tls_curl_rc=$?) and include it in the output so TLS "
            "handshake failures are distinguishable from HTTP errors."
        )

    def test_tls_success_requires_both_http_status_and_zero_curl_rc(self):
        """TLS success check must gate on BOTH HTTP status AND curl exit code,
        not HTTP status alone."""
        content = _read_script()
        # Look for a condition that tests tls_curl_rc alongside TLS_STATUS
        # e.g.: if [ "$tls_curl_rc" -eq 0 ] && [ "$TLS_STATUS" != "000" ]
        # We require tls_curl_rc to appear in a conditional alongside TLS_STATUS
        has_rc_in_condition = re.search(
            r'if\b[^;{]*tls_curl_rc[^;{]*TLS_STATUS|'
            r'if\b[^;{]*TLS_STATUS[^;{]*tls_curl_rc',
            content, re.IGNORECASE
        )
        assert has_rc_in_condition, (
            "TLS result check must test both tls_curl_rc and TLS_STATUS. "
            "HTTP nonzero alone is insufficient — a TLS handshake failure "
            "yields curl exit code != 0 regardless of HTTP status."
        )

    def test_ca_file_readability_check(self):
        """Script must verify the CA file is readable before using it
        (e.g. with [ -r \"$CACERT\" ] or [ -f ... ] guard)."""
        content = _read_script()
        # Look for a readability/file-existence test on the CA variable
        assert re.search(
            r'\[\s*-[rf]\s+["\$].*[Cc][Aa][Cc][Ee][Rr][Tt]|'
            r'\[\s*-[rf]\s+["\$].*[Cc][Aa]_[Ff][Ii][Ll][Ee]|'
            r'\[\s*-[rf]\s+"\$\{?CACERT|'
            r'\[\s*-[rf]\s+"\$\{?CA_FILE|'
            r'\[\s*-[rf]\s+"\$\{?CA_CERT',
            content, re.IGNORECASE
        ), (
            "No readability check (-r or -f) found for CA cert file variable. "
            "The script must verify the file exists and is readable before "
            "passing it to --cacert."
        )


# ---------------------------------------------------------------------------
# Output safety
# ---------------------------------------------------------------------------

class TestOutputSafety:
    """Script must not dump raw response headers or bodies."""

    def test_no_verbose_flag(self):
        """curl -v / --verbose must not appear (leaks headers/secrets)."""
        content = _read_script()
        assert not re.search(r"\bcurl\b[^\n]*(?:\s-v\b|\s--verbose)", content), (
            "curl -v / --verbose found — must not dump headers"
        )

    def test_no_include_flag(self):
        """curl -i / --include must not appear (dumps response headers)."""
        content = _read_script()
        assert not re.search(r"\bcurl\b[^\n]*(?:\s-i\b|\s--include)", content), (
            "curl -i / --include found — must not dump response headers"
        )


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------

class TestRobustness:
    """Script must have basic shell robustness settings."""

    def test_set_e_or_errexit(self):
        """set -e or set -o errexit must appear."""
        content = _read_script()
        assert re.search(
            r"set\s+.*-[a-zA-Z]*e[a-zA-Z]*|set\s+-o\s+errexit",
            content
        ), "set -e / set -o errexit not found"

    def test_set_u_or_nounset(self):
        """set -u or set -o nounset must appear."""
        content = _read_script()
        assert re.search(
            r"set\s+.*-[a-zA-Z]*u[a-zA-Z]*|set\s+-o\s+nounset",
            content
        ), "set -u / set -o nounset not found"


# ---------------------------------------------------------------------------
# CA path propagation — Task 1 operability fix
# ---------------------------------------------------------------------------

class TestCACertDefault:
    """CACERT must have a safe in-script default so PBS jobs work without
    -V / -v on qsub.  The default path is
    $HOME/polaris-headscale-preflight/caddy-root.crt and callers can override
    it via 'qsub ... -v CACERT=/alternate/path'."""

    EXPECTED_DEFAULT_SUFFIX = "polaris-headscale-preflight/caddy-root.crt"

    def test_cacert_has_shell_default(self):
        """CACERT must use the ${CACERT:-<default>} pattern so the script
        works without the caller exporting CACERT or passing qsub -V."""
        content = _read_script()
        assert re.search(
            r"\$\{CACERT:-[^}]+\}",
            content
        ), (
            "CACERT variable must have a safe default via "
            "'${CACERT:-$HOME/polaris-headscale-preflight/caddy-root.crt}'. "
            "Without it, qsub -V or -v CACERT=... is the only way to "
            "propagate the value into the compute node environment, which "
            "is not obvious and breaks by default."
        )

    def test_cacert_default_references_expected_path(self):
        """The default CA path must include the expected filename suffix
        '$HOME/polaris-headscale-preflight/caddy-root.crt'."""
        content = _read_script()
        m = re.search(r"\$\{CACERT:-([^}]+)\}", content)
        assert m, (
            "CACERT must use ${CACERT:-<default>} shell expansion. "
            "No such pattern found."
        )
        default_val = m.group(1)
        assert self.EXPECTED_DEFAULT_SUFFIX in default_val, (
            f"CACERT default '{default_val}' must include "
            f"'{self.EXPECTED_DEFAULT_SUFFIX}'."
        )

    def test_usage_comment_shows_default_invocation(self):
        """Usage comment must show plain 'qsub -A ...' (default CA path) form."""
        content = _read_script()
        # e.g.:   qsub -A "$ALCF_PROJECT" deploy/polaris/probe-egress.pbs
        # (no -v CACERT, no -V; the default is already baked in)
        assert re.search(
            r"qsub\s+-A\s+[\"']?\$(?:\{ALCF_PROJECT\}|ALCF_PROJECT)[\"']?\s+[^\n]*probe-egress\.pbs",
            content
        ), (
            "Usage comment must include a plain invocation like:\n"
            "  qsub -A \"$ALCF_PROJECT\" deploy/polaris/probe-egress.pbs\n"
            "showing callers that no -v CACERT or -V is needed for the "
            "default CA path."
        )

    def test_usage_comment_shows_override_invocation(self):
        """Usage comment must show '-v CACERT=...' override form for callers
        who need a non-default CA certificate."""
        content = _read_script()
        # e.g.:  qsub -A "$ALCF_PROJECT" -v CACERT=/alternate/path ...
        assert re.search(
            r"qsub\s+.*-v\s+CACERT=",
            content
        ), (
            "Usage comment must include an override invocation like:\n"
            "  qsub -A \"$ALCF_PROJECT\" -v CACERT=/alternate/path ...\n"
            "so callers know how to supply a non-default CA certificate "
            "without needing qsub -V."
        )

    def test_no_awkward_nested_quoting_in_comments(self):
        r"""Comment lines must not contain nested shell quoting like
        'qsub -A \"$ALCF_PROJECT\"' (backslash-escaped quotes inside
        a comment) — plain qsub -A "$ALCF_PROJECT" is correct."""
        content = _read_script()
        for lineno, line in enumerate(content.splitlines(), 1):
            stripped = line.lstrip()
            if not stripped.startswith("#"):
                continue
            # A comment line that contains \" is a red flag for copy-paste
            # of shell code that was escaped for a different quoting context.
            if re.search(r'\\"', line):
                raise AssertionError(
                    f"Line {lineno} has awkward backslash-escaped quotes "
                    f"inside a comment:\n  {line}\n"
                    "Replace '\\\"' with plain '\"' in comment text."
                )
