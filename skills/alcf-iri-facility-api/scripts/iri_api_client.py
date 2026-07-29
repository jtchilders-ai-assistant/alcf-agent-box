#!/usr/bin/env python3
"""Reusable thin client for the ALCF IRI Facility API (api.alcf.anl.gov).

Handles the async filesystem task model (submit -> poll /task/{id}) and provides
small wrappers around the documented endpoints. Requires a valid access token from
alcf_facility_api_globus_token.py (see the alcf-iri-facility-api SKILL.md for the
Globus interactive-auth recipe).

Usage:
    from iri_api_client import IRI
    api = IRI.from_token_command()          # runs `get_access_token` via the auth script
    # or: api = IRI(token="Agx...")         # pass a token string directly
    print(api.resources())                  # no-auth status
    print(api.projects())                   # auth: your ALCF projects
    print(api.ls(IRI.HOME, "/home/<you>"))  # async fs op, auto-polled
"""
import json
import subprocess
import time
import urllib.parse
import urllib.request

BASE = "https://api.alcf.anl.gov/api/v1"

# Verified-live resource IDs (re-verify with resources() if in doubt)
RESOURCES = {
    "polaris": "55c1c993-1124-47f9-b823-514ba3849a9a",
    "crux":    "8b9b42f7-572a-4909-8472-a0453436304c",
    "aurora":  "0325fc07-6fb7-4453-b772-3d5030b2df72",
    "sophia":  "9674c7e1-aecc-4dbb-bf01-c9197e027cd6",
    "eagle":   "1c3ad9d4-2e91-42bc-becb-72b1fde1235c",
    "home":    "6115bd2c-957a-4543-abff-5fae52992ff2",
}


class IRI:
    POLARIS = RESOURCES["polaris"]
    EAGLE = RESOURCES["eagle"]
    HOME = RESOURCES["home"]

    def __init__(self, token=None):
        self.token = token

    @classmethod
    def from_token_command(cls, script="alcf_facility_api_globus_token.py", python="python"):
        out = subprocess.check_output([python, script, "get_access_token"], text=True).strip()
        return cls(token=out)

    def _req(self, method, path, params=None, body=None, auth=True):
        url = f"{BASE}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if auth and self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read().decode()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            return e.code, {"error": e.read().decode()[:500]}

    # ---- no-auth status ----
    def resources(self):
        return self._req("GET", "/status/resources", auth=False)[1]

    def facility(self):
        return self._req("GET", "/facility", auth=False)[1]

    # ---- account ----
    def projects(self):
        return self._req("GET", "/account/projects")[1]

    def allocations(self, project_id):
        return self._req("GET", f"/account/projects/{project_id}/project_allocations")[1]

    # ---- compute ----
    def submit_job(self, resource_id, body):
        return self._req("POST", f"/compute/job/{resource_id}", body=body)

    def job_status(self, resource_id, job_id, historical=True):
        return self._req("GET", f"/compute/status/{resource_id}/{job_id}",
                         params={"historical": str(historical).lower()})[1]

    def cancel_job(self, resource_id, job_id):
        return self._req("DELETE", f"/compute/cancel/{resource_id}/{job_id}")[0]  # 204 = accepted

    # ---- async filesystem (submit -> poll task) ----
    def _task_poll(self, task_id, tries=30, delay=1.0):
        last = None
        for _ in range(tries):
            _, last = self._req("GET", f"/task/{task_id}")
            if last.get("status") in ("completed", "failed", "error", "cancelled"):
                return last
            time.sleep(delay)
        return last

    def _fs(self, method, verb, resource_id, params=None, body=None):
        """Submit a filesystem op and poll its task to completion.
        NOTE: a 200 submit does NOT mean success — the task can still fail
        (bad path, per-identity allowlist). Always check the returned task's status/result."""
        code, resp = self._req(method, f"/filesystem/{verb}/{resource_id}", params=params, body=body)
        if code >= 400:
            return {"submit_http": code, "error": resp}
        tid = resp.get("task_id") or resp.get("id")
        return self._task_poll(tid) if tid else {"submit_http": code, "resp": resp}

    def ls(self, resource_id, path):
        return self._fs("GET", "ls", resource_id, params={"path": path})

    def mkdir(self, resource_id, path, parent=False):
        return self._fs("POST", "mkdir", resource_id, body={"path": path, "parent": parent})

    def head(self, resource_id, path, lines=10):
        return self._fs("GET", "head", resource_id, params={"path": path, "lines": lines})

    def view(self, resource_id, path, size=100, offset=0):
        return self._fs("GET", "view", resource_id, params={"path": path, "size": size, "offset": offset})

    def rm(self, resource_id, path):
        return self._fs("DELETE", "rm", resource_id, params={"path": path})


if __name__ == "__main__":
    api = IRI()
    for r in api.resources():
        print(f"{r['name']:20s} {r['id']}  {r['current_status']}")
