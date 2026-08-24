#!/usr/bin/env python3
"""Tests for facility-API error surfacing.

Regression cover for a real failure seen in a live session: with an expired
IRI Globus token, `alcf_facility.py jobs` printed "No jobs found on polaris
(active only)." and exited 0. The agent reported an empty queue to the user
while the truth was HTTP 401 — and the remediation text ("log out at
app.globus.org/logout, re-authenticate with alcf.anl.gov") had been cut off by
a [:500] head-slice of the error body followed by a [:200] slice at print time.

Two invariants are locked in here:
  1. an API error is never reported as an empty result, and exits non-zero;
  2. error bodies are not truncated — the actionable tail survives.

No network: the IRI client is stubbed.

Run:  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, os.path.join(REPO, "skills", "alcf-iri-facility-api", "scripts"))

import alcf_facility as af  # noqa: E402
import iri_api_client as iri  # noqa: E402


# The real 401 body ALCF returns. The half that tells you how to recover is at
# the END — which is exactly why head-truncating it was harmful.
REAL_401_BODY = json.dumps({
    "type": "https://api.alcf.anl.gov/errors/unauthorized",
    "status": 401,
    "title": "Unauthorized",
    "detail": (
        "Facility Specific authentication failed: 401: Authentication not "
        "compliant with Globus policy, likely due to a high-assurance timeout. "
        "Please logout by visiting https://app.globus.org/logout and "
        "re-authenticate. Use an incognito browser or clear browser cache "
        "before re-authenticating. Make sure you authenticate with alcf.anl.gov."
    ),
})


class _Args:
    """Stand-in for the argparse namespace cmd_jobs consumes."""

    def __init__(self, **kw):
        self.cluster = "polaris"
        self.historical = False
        self.limit = 100
        self.json = False
        self.__dict__.update(kw)


class _StubApi:
    def __init__(self, resp):
        self._resp = resp

    def job_statuses(self, *a, **kw):
        return self._resp


class _Capture:
    """Run cmd_jobs with a stubbed client, capturing streams + exit code."""

    def __init__(self, resp, args=None):
        self.resp, self.args = resp, args or _Args()

    def run(self):
        out, err = io.StringIO(), io.StringIO()
        real = af._authed_client
        af._authed_client = lambda: _StubApi(self.resp)  # type: ignore[assignment]
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = af.cmd_jobs(self.args)
        finally:
            af._authed_client = real  # type: ignore[assignment]
        return rc, out.getvalue(), err.getvalue()


class ErrorBodyNotTruncatedTests(unittest.TestCase):
    """_req must return the FULL error body, not a head slice."""

    def test_full_body_survives(self):
        long_detail = "A" * 3000 + "REMEDIATION_TAIL"
        body = json.dumps({"status": 401, "detail": long_detail}).encode()

        class _Err(urllib.error.HTTPError):
            def __init__(self):
                super().__init__("u", 401, "Unauthorized", {}, io.BytesIO(body))

        def boom(*a, **kw):
            raise _Err()

        real = iri.urllib.request.urlopen
        iri.urllib.request.urlopen = boom  # type: ignore[assignment]
        try:
            code, resp = iri.IRI(token="t")._req("GET", "/x")
        finally:
            iri.urllib.request.urlopen = real  # type: ignore[assignment]

        self.assertEqual(code, 401)
        self.assertEqual(resp["http_status"], 401)
        # The tail is the actionable part — it must not be cut off.
        self.assertIn("REMEDIATION_TAIL", resp["error"])
        self.assertGreater(len(resp["error"]), 3000)


class JobsAuthFailureTests(unittest.TestCase):
    """A 401 is not an empty queue."""

    def setUp(self):
        self.resp = {"error": REAL_401_BODY, "http_status": 401}

    def test_exits_nonzero(self):
        rc, _, _ = _Capture(self.resp).run()
        self.assertNotEqual(rc, 0, "auth failure must not exit 0")

    def test_never_claims_no_jobs(self):
        _, out, err = _Capture(self.resp).run()
        combined = out + err
        self.assertNotIn("No jobs found", combined,
                         "a 401 must never render as an empty job list")

    def test_says_auth_failed_and_unknown(self):
        _, _, err = _Capture(self.resp).run()
        self.assertIn("AUTH FAILED", err)
        self.assertIn("UNKNOWN", err)

    def test_keeps_the_actionable_remediation(self):
        _, _, err = _Capture(self.resp).run()
        # the tail of the real body, previously lost to truncation
        self.assertIn("app.globus.org/logout", err)
        self.assertIn("alcf.anl.gov", err)
        self.assertIn("incognito", err)

    def test_names_the_right_login_of_the_three(self):
        _, _, err = _Capture(self.resp).run()
        self.assertIn("alcf_facility_api_globus_token.py", err)


class JobsHappyPathTests(unittest.TestCase):
    """The error branch must not disturb normal operation."""

    def test_genuinely_empty_is_still_exit_0(self):
        rc, out, _ = _Capture({"jobs": []}).run()
        self.assertEqual(rc, 0)
        self.assertIn("No jobs found", out)

    def test_jobs_are_listed(self):
        resp = {"jobs": [{"id": "123456.polaris-pbs-01",
                          "status": {"state": "running", "exit_code": None}}]}
        rc, out, _ = _Capture(resp).run()
        self.assertEqual(rc, 0)
        self.assertIn("123456", out)
        self.assertIn("running", out)

    def test_json_mode_passes_error_through_untouched(self):
        resp = {"error": REAL_401_BODY, "http_status": 401}
        rc, out, _ = _Capture(resp, _Args(json=True)).run()
        self.assertEqual(rc, 0)
        self.assertIn("app.globus.org/logout", out)

    def test_non_401_error_also_flagged(self):
        rc, out, err = _Capture({"error": "boom", "http_status": 500}).run()
        self.assertNotEqual(rc, 0)
        self.assertIn("API ERROR", err)
        self.assertNotIn("No jobs found", out + err)

    def test_error_without_status_does_not_crash(self):
        rc, _, err = _Capture({"error": "mystery"}).run()
        self.assertNotEqual(rc, 0)
        self.assertIn("API ERROR", err)


if __name__ == "__main__":
    unittest.main()
