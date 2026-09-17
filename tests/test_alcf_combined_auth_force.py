import ast
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "alcf_combined_auth.py"


def test_interactive_authentication_passes_force_option_to_globus_login():
    """`authenticate --force` must renew the 30-day high-assurance session."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    fn = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_cli_authenticate"
    )
    assert fn.args.args[0].arg == "force"
    login_calls = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "login"
    ]
    assert len(login_calls) == 1
    force = next((kw.value for kw in login_calls[0].keywords if kw.arg == "force"), None)
    assert isinstance(force, ast.Name) and force.id == "force"


def test_authenticate_parser_defines_force_flag_and_passes_it_to_handler():
    body = SOURCE.read_text(encoding="utf-8")
    assert 'auth = sub.add_parser("authenticate"' in body
    assert 'auth.add_argument("--force", action="store_true"' in body
    assert "return _cli_authenticate(force=args.force)" in body
