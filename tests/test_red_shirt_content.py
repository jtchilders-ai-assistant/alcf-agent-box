from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_compute_image_is_pinned_and_non_root():
    text = (ROOT / "Dockerfile.red-shirt-polaris").read_text()
    assert "nousresearch/hermes-agent:v2026.9.14@sha256:" in text
    assert "tailscale/tailscale:v1.88.3@sha256:" in text
    assert "USER hermes" in text
    assert 'ENTRYPOINT ["/opt/red-shirt-polaris/entrypoint.sh"]' in text


def test_ci_publishes_compute_image_for_both_architectures():
    text = (ROOT / ".github/workflows/build.yml").read_text()
    assert "alcf-red-shirt-polaris" in text
    assert "file: Dockerfile.red-shirt-polaris" in text
    assert "platforms: linux/amd64,linux/arm64" in text
