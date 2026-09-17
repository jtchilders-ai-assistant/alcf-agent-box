from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "polaris" / "authenticate-red-shirt.sh"


def test_authentication_script_is_safe_and_persistent():
    body = SCRIPT.read_text(encoding="utf-8")
    assert 'BASE_DIR="${RED_SHIRT_BASE_DIR:-$HOME/red-shirt-polaris}"' in body
    assert 'SIF="${RED_SHIRT_SIF:-$BASE_DIR/red-shirt-polaris-current.sif}"' in body
    assert '--env HOME=/opt/data' in body
    assert '--bind "$BASE_DIR/home:/opt/data"' in body
    assert 'ALCF_ENABLE_IRI="$ENABLE_IRI"' in body
    assert 'ALCF_ENABLE_GLOBUS_COMPUTE="$ENABLE_COMPUTE"' in body
    assert '/opt/red-shirt-polaris/alcf_combined_auth.py "$ACTION"' in body
    assert "get_access_token" not in body
