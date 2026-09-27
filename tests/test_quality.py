import ast
import json
from pathlib import Path

from fba.contracts.config import LeagueRules, ModelConfig, SeasonConfig
from fba.contracts.data import IdentityMap, ManualAdjustments


def root():
    return Path(__file__).resolve().parents[1]


def test_runtime_types_own_schemas():
    for name, model in (
        ("league", LeagueRules),
        ("season", SeasonConfig),
        ("model", ModelConfig),
        ("identity-map", IdentityMap),
        ("manual-adjustments", ManualAdjustments),
    ):
        expected = model.model_json_schema()
        actual = json.loads((root() / "design/schemas" / f"{name}.schema.json").read_bytes())
        assert actual == expected


def test_core_dependency_direction_and_no_io_or_mutable_globals():
    allowed = {"collections", "datetime", "fractions", "typing", "fba.contracts"}
    forbidden_calls = {"open", "eval", "exec", "__import__", "print", "input"}
    for path in (root() / "src/fba/core").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module in allowed or node.module.startswith("fba.contracts."), path
            if isinstance(node, ast.Import):
                assert all(n.name in allowed for n in node.names), path
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in forbidden_calls, path
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                assert not isinstance(node.value, (ast.List, ast.Dict, ast.Set, ast.Call)), path


def test_core_contains_no_league_literals():
    forbidden = {"PG", "SG", "SF", "PF", "C", "UTIL", "FG%", "FT%", "A/T", "OREB", "DD"}
    for path in (root() / "src/fba/core").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Constant):
                assert node.value not in forbidden if isinstance(node.value, str) else True, path
                if isinstance(node.value, int):
                    # Language/domain cardinalities; no league budget, team count or roster size.
                    assert node.value in {0, 1, 2}, (path, node.lineno, node.value)


def test_every_runtime_module_is_import_reachable_from_cli():
    paths = {
        ".".join(p.relative_to(root() / "src").with_suffix("").parts): p
        for p in (root() / "src/fba").rglob("*.py")
        if p.name != "__init__.py"
    }
    reached = set()
    pending = ["fba.apps.cli"]
    while pending:
        name = pending.pop()
        if name in reached or name not in paths:
            continue
        reached.add(name)
        for node in ast.walk(ast.parse(paths[name].read_text())):
            if isinstance(node, ast.ImportFrom) and node.module:
                pending.append(node.module)
                pending.extend(node.module + "." + alias.name for alias in node.names)
    assert set(paths) == reached


def test_no_duplicate_nontrivial_function_bodies():
    bodies = {}
    for path in (root() / "src/fba").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and len(node.body) >= 5:
                normalized = ast.dump(
                    ast.Module(body=node.body, type_ignores=[]), include_attributes=False
                )
                assert normalized not in bodies, (path, node.name, bodies.get(normalized))
                bodies[normalized] = (path.name, node.name)
