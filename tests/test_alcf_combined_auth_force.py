import ast
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "alcf_combined_auth.py"


def test_interactive_authentication_forces_new_globus_login():
    """`authenticate` must renew the 30-day high-assurance session, not reuse cache."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    fn = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_cli_authenticate"
    )
    login_calls = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "login"
    ]
    assert len(login_calls) == 1
    force = next((kw.value for kw in login_calls[0].keywords if kw.arg == "force"), None)
    assert isinstance(force, ast.Constant) and force.value is True
