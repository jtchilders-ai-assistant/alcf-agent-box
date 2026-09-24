"""User-facing authentication messages use the combined token authority."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import alcf_remote_bash as rb  # noqa: E402


class _Combined:
    TOKENS_PATH = "/opt/data/.globus/app/combined/tokens.json"

    @staticmethod
    def compute_enabled():
        return True

    @staticmethod
    def has_tokens():
        return True


class CombinedAuthMessagingTests(unittest.TestCase):
    def setUp(self):
        self.original = rb._combined
        self.original_import_sdk = rb._import_sdk
        self.original_has_tokens = rb._has_tokens
        rb._combined = _Combined()
        rb._import_sdk = lambda: None

    def tearDown(self):
        rb._combined = self.original
        rb._import_sdk = self.original_import_sdk
        rb._has_tokens = self.original_has_tokens

    def test_check_names_combined_token_store(self):
        output = StringIO()
        with redirect_stdout(output):
            result = rb.cmd_check(None)
        self.assertEqual(result, 0)
        self.assertIn("/opt/data/.globus/app/combined/tokens.json", output.getvalue())
        self.assertNotIn("~/.globus_compute/storage.db", output.getvalue())

    def test_preflight_recommends_combined_reauthentication(self):
        rb._has_tokens = lambda: False
        error = StringIO()
        with redirect_stderr(error):
            result = rb._preflight_run(None)
        self.assertEqual(result, 3)
        self.assertIn("alcf_combined_auth.py authenticate --force", error.getvalue())
        self.assertNotIn("\\\n\n", error.getvalue())
        self.assertNotIn("alcf_remote_bash.py authenticate", error.getvalue())


if __name__ == "__main__":
    unittest.main()
