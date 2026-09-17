from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile.red-shirt-polaris"


def test_red_shirt_image_installs_globus_sdk_into_hermes_interpreter():
    body = DOCKERFILE.read_text(encoding="utf-8")
    assert (
        "uv pip install --python /opt/hermes/.venv/bin/python --no-cache globus-sdk"
        in body
    )
