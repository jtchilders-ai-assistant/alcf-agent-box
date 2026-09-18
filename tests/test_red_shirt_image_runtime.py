from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile.red-shirt-polaris"


def test_red_shirt_image_installs_globus_sdk_into_hermes_interpreter():
    body = DOCKERFILE.read_text(encoding="utf-8")
    assert (
        "uv pip install --python /opt/hermes/.venv/bin/python --no-cache globus-sdk"
        in body
    )


def test_red_shirt_image_copies_config_renderer_runtime_dependencies():
    body = DOCKERFILE.read_text(encoding="utf-8")
    assert "COPY scripts/red_shirt_config.py /opt/red-shirt-polaris/red_shirt_config.py" in body
    assert "COPY scripts/populate_models.py /opt/red-shirt-polaris/populate_models.py" in body
    assert "COPY scripts/resolve_context_length.py /opt/red-shirt-polaris/resolve_context_length.py" in body


def test_red_shirt_image_patches_and_compiles_standard_a2a_proxy_support():
    body = DOCKERFILE.read_text(encoding="utf-8")
    assert "COPY scripts/patch_hermes_a2a_proxy.py" in body
    assert "python -m py_compile /opt/hermes/plugins/platforms/a2a/tools.py" in body


def test_pbs_maps_container_home_to_persistent_hermes_home():
    body = (ROOT / "deploy" / "polaris" / "red-shirt-polaris.pbs").read_text(
        encoding="utf-8"
    )
    assert 'export APPTAINERENV_HOME="/opt/data"' in body
