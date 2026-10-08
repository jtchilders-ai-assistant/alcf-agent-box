from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class LoopbackHttpContractTests(unittest.TestCase):
    def test_caddy_serves_plain_http_without_internal_tls(self):
        caddy = (ROOT / "config" / "Caddyfile").read_text()
        self.assertIn("http://localhost:${ALCF_DASHBOARD_PORT}", caddy)
        self.assertNotIn("tls internal", caddy)
        self.assertNotIn("skip_install_trust", caddy)

    def test_readme_uses_loopback_only_publish_and_http_url(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn("-p 127.0.0.1:8787:8787", readme)
        self.assertIn("http://localhost:8787", readme)
        command_blocks = readme.split("```bash")[1:]
        for block in command_blocks:
            command = block.split("```", 1)[0]
            self.assertNotIn("-p 8787:8787", command)
        self.assertNotIn("self-signed", readme.lower())

    def test_image_docs_do_not_claim_https_or_self_signed_tls(self):
        for relative in ("Dockerfile", "docs/DESIGN.md"):
            body = (ROOT / relative).read_text()
            self.assertNotIn("self-signed", body.lower(), relative)
            self.assertNotIn("https://localhost", body, relative)

    def test_podman_publishes_only_to_host_loopback(self):
        readme = (ROOT / "README.md").read_text()
        podman = readme.split("## Run locally with Podman", 1)[1].split("## ", 1)[0]
        self.assertIn("-p 127.0.0.1:8787:8787", podman)
        self.assertNotIn("-p 8787:8787", podman)

    def test_apptainer_uses_host_network_without_port_mapping(self):
        readme = (ROOT / "README.md").read_text()
        apptainer = readme.split("## Run locally with Apptainer", 1)[1].split("## ", 1)[0]
        self.assertIn("host network", apptainer)
        self.assertNotIn("--net", apptainer)
        self.assertNotIn("portmap=", apptainer)


if __name__ == "__main__":
    unittest.main()
