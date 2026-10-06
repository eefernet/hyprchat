"""Watch registration must not silently omit newly added frontend sources."""
import ast
from pathlib import Path


def test_all_frontend_build_inputs_are_watched():
    source = Path(__file__).resolve().parents[2] / 'deploy_monitor.py'
    tree = ast.parse(source.read_text())
    namespace = {'REMOTE_FRONTEND': '/frontend/'}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'FRONTEND_SRC_FILES' for t in node.targets):
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
    namespace['WATCHED'] = {}
    updates = [n for n in tree.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
               and isinstance(n.value.func, ast.Attribute) and isinstance(n.value.func.value, ast.Name)
               and n.value.func.value.id == 'WATCHED' and n.value.func.attr == 'update']
    exec(compile(ast.Module(body=updates, type_ignores=[]), str(source), 'exec'), namespace)
    for path in namespace['FRONTEND_SRC_FILES']:
        assert namespace['WATCHED'][path] == ('Frontend (build)', '/frontend/', False)
