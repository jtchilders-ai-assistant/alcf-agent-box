"""Release contract for the official alcf-tokens authentication integration."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class AlcfTokensIntegrationContractTests(unittest.TestCase):
    def test_custom_authentication_scripts_are_removed(self):
        removed = [
            "scripts/alcf_combined_auth.py",
            "scripts/inference_auth_token.py",
            "scripts/alcf_facility_api_globus_token.py",
        ]
        self.assertEqual(
            [path for path in removed if (ROOT / path).exists()],
            [],
        )

    def test_image_pins_released_alcf_tokens(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn("alcf-tokens==0.3.0", dockerfile)
        for script in (
            "alcf_combined_auth.py",
            "inference_auth_token.py",
            "alcf_facility_api_globus_token.py",
        ):
            self.assertNotIn(f"COPY scripts/{script}", dockerfile)

    def test_entrypoint_uses_official_cli_for_login_and_tokens(self):
        entrypoint = (ROOT / "scripts/entrypoint.sh").read_text()
        self.assertIn('ALCF_TOKENS="$VENV_BIN/alcf-tokens"', entrypoint)
        self.assertIn('"$ALCF_TOKENS" login', entrypoint)
        self.assertIn('"$ALCF_TOKENS" get-token inference', entrypoint)
        self.assertNotIn("alcf_combined_auth.py", entrypoint)
        self.assertNotIn("inference_auth_token.py", entrypoint)
        self.assertNotIn("alcf_facility_api_globus_token.py", entrypoint)

    def test_runtime_consumers_import_official_python_api(self):
        expected = {
            "scripts/alcf_facility.py": "from alcf_tokens.auth import get_access_token",
            "scripts/iri_hello_world.py": "from alcf_tokens.auth import get_access_token",
            "scripts/populate_models.py": "from alcf_tokens.auth import get_access_token",
            "scripts/resolve_context_length.py": "from alcf_tokens.auth import get_access_token",
            "scripts/alcf_remote_bash.py": "from alcf_tokens.auth import get_service_authorizer",
        }
        for relative, marker in expected.items():
            body = (ROOT / relative).read_text()
            self.assertIn(marker, body, relative)
            self.assertNotIn("alcf_combined_auth", body, relative)
            self.assertNotIn("inference_auth_token.py", body, relative)
            self.assertNotIn("alcf_facility_api_globus_token.py", body, relative)

    def test_baked_skills_do_not_recommend_removed_auth_helpers(self):
        shipped = [
            "skills/alcf-inference-service/SKILL.md",
            "skills/alcf-inference-service/scripts/probe_inference.sh",
            "skills/alcf-iri-facility-api/SKILL.md",
            "skills/alcf-iri-facility-api/scripts/iri_api_client.py",
            "skills/alcf-facility-status-and-jobs/SKILL.md",
        ]
        removed = (
            "alcf_combined_auth.py",
            "inference_auth_token.py",
            "alcf_facility_api_globus_token.py",
        )
        for relative in shipped:
            body = (ROOT / relative).read_text()
            self.assertIn("alcf-token", body, relative)
            for obsolete in removed:
                self.assertNotIn(obsolete, body, relative)

    def test_user_facing_runtime_docs_name_official_login(self):
        for relative in ("README.md", "docs/DESIGN.md", "memory/MEMORY.md"):
            body = (ROOT / relative).read_text()
            self.assertIn("alcf-tokens login", body, relative)
            self.assertNotIn("alcf_combined_auth.py", body, relative)


if __name__ == "__main__":
    unittest.main()
