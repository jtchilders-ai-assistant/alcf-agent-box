"""Offline tests for alcf_notify.build_request (no network)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "scripts"))

import alcf_notify as n  # noqa: E402


class BuildRequestTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.pop(k) for k in
                       ("ALCF_NTFY_TOPIC", "ALCF_NTFY_SERVER") if k in os.environ}

    def tearDown(self):
        os.environ.update(self._saved)

    def test_requires_topic(self):
        with self.assertRaises(ValueError):
            n.build_request("hi")

    def test_topic_and_default_server(self):
        url, body, headers = n.build_request("job started", topic="alcf-x7")
        self.assertEqual(url, "https://ntfy.sh/alcf-x7")
        self.assertEqual(body, b"job started")
        self.assertEqual(headers, {})

    def test_env_config(self):
        os.environ["ALCF_NTFY_TOPIC"] = "t0pic"
        os.environ["ALCF_NTFY_SERVER"] = "https://ntfy.example/"
        url, _, _ = n.build_request("x")
        self.assertEqual(url, "https://ntfy.example/t0pic")

    def test_metadata_headers(self):
        _, _, headers = n.build_request("done", topic="t",
                                        title="Pepper build", priority="high",
                                        tags="white_check_mark")
        self.assertEqual(headers, {"Title": "Pepper build", "Priority": "high",
                                   "Tags": "white_check_mark"})

    def test_invalid_priority_rejected(self):
        with self.assertRaises(ValueError):
            n.build_request("x", topic="t", priority="asap")

    def test_non_latin1_title_moves_to_body(self):
        _, body, headers = n.build_request("done", topic="t", title="构建完成")
        self.assertNotIn("Title", headers)
        self.assertEqual(body.decode("utf-8"), "构建完成\ndone")

    def test_utf8_message_body(self):
        _, body, _ = n.build_request("nœud x3108 → OK", topic="t")
        self.assertEqual(body.decode("utf-8"), "nœud x3108 → OK")


if __name__ == "__main__":
    unittest.main(verbosity=2)
