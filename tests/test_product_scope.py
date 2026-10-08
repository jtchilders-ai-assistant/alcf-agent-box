"""Release-contract tests for the user-facing Agent in a Box repository."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ProductScopeTests(unittest.TestCase):
    def test_experimental_artifacts_are_not_shipped(self):
        forbidden = [
            "Dockerfile.headscale-probe",
            "Dockerfile.red-shirt-polaris",
            "config/red-shirt-polaris",
            "deploy/polaris",
            "docs/CAPABILITY-IDEAS.md",
            "docs/REMOTE-AGENT-ARCHITECTURE.md",
            "docs/red-shirt-environment-catalog.md",
            "docs/superpowers",
            "scripts/connect_proxy.py",
            "scripts/headscale_probe.sh",
            "scripts/patch_hermes_a2a_proxy.py",
        ]
        forbidden.extend(str(path.relative_to(ROOT)) for path in ROOT.glob("scripts/red_shirt_*"))
        present = sorted(path for path in forbidden if (ROOT / path).exists())
        self.assertEqual(present, [], f"experimental artifacts remain: {present}")

    def test_build_workflow_publishes_only_the_laptop_image(self):
        workflow = (ROOT / ".github/workflows/build.yml").read_text()
        self.assertNotIn("headscale", workflow.lower())
        self.assertNotIn("red-shirt", workflow.lower())
        self.assertEqual(workflow.count("docker/build-push-action@"), 1)

    def test_readme_describes_one_combined_login(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn("ONE combined Globus login", readme)
        self.assertNotIn("a **separate** Globus login", readme)
        self.assertNotIn("the second (IRI) Globus login", readme)
        self.assertNotIn("the separate **IRI Facility API**", readme)

    def test_readme_documents_supported_local_runtimes(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn("## Run locally with Podman", readme)
        self.assertIn("## Run locally with Apptainer", readme)
        self.assertIn("podman run -it --rm", readme)
        self.assertIn("apptainer pull alcf-agent.sif", readme)
        self.assertIn("docker://ghcr.io/jtchilders-ai-assistant/alcf-agent:latest", readme)
        self.assertIn("The agent runs on your local machine", readme)
        self.assertIn("Globus Compute", readme)

    def test_apptainer_instructions_preserve_private_persistent_state(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn("chmod 700 \"$HOME/.local/share/alcf-agent\"", readme)
        self.assertIn("--containall", readme)
        self.assertIn("--no-mount cwd", readme)
        self.assertIn('APPTAINER_HOME="$HOME/.local/share/alcf-agent"', readme)
        self.assertIn('--home "$APPTAINER_HOME:$HOME"', readme)
        self.assertIn("--cwd /opt/data", readme)
        self.assertIn('"$HOME/.local/share/alcf-agent:/opt/data"', readme)
        self.assertIn("APPTAINERENV_ALCF_DASHBOARD_PASSWORD", readme)
        self.assertIn("apptainer run", readme)
        self.assertNotIn("module load apptainer", readme)

    def test_apptainer_command_uses_oci_entrypoint(self):
        readme = (ROOT / "README.md").read_text()
        apptainer = readme.split("## Run locally with Apptainer", 1)[1].split("## ", 1)[0]
        self.assertIn("apptainer run", apptainer)
        self.assertNotIn("/opt/alcf/entrypoint.sh", apptainer)

    def test_user_repo_does_not_ship_ntfy_experiment(self):
        self.assertFalse((ROOT / "scripts/alcf_notify.py").exists())
        self.assertFalse((ROOT / "skills/alcf-background-tasks").exists())
        dockerfile = (ROOT / "Dockerfile").read_text()
        readme = (ROOT / "README.md").read_text()
        memory = (ROOT / "memory/MEMORY.md").read_text()
        for content in (dockerfile, readme, memory):
            self.assertNotIn("ntfy", content.lower())
            self.assertNotIn("ALCF_NTFY", content)


if __name__ == "__main__":
    unittest.main()
