import ast
import difflib
import json
from pathlib import Path

import lizard

from fba.contracts.archive import ForecastArchive
from fba.contracts.auction import DraftState
from fba.contracts.backtest import ReplayInput
from fba.contracts.config import LeagueRules, ModelDocument, SeasonConfig
from fba.contracts.data import IdentityMap, ManualAdjustments


def root():
    return Path(__file__).resolve().parents[1]


def test_runtime_types_own_schemas():
    schemas = (
        ("forecast", ForecastArchive),
        ("draft", DraftState),
        ("replay", ReplayInput),
        ("league", LeagueRules),
        ("season", SeasonConfig),
        ("model", ModelDocument),
        ("identity-map", IdentityMap),
        ("manual-adjustments", ManualAdjustments),
    )
    assert {p.name for p in (root() / "design/schemas").glob("*.json")} == {
        f"{name}.schema.json" for name, _ in schemas
    }
    for name, model in schemas:
        expected = model.model_json_schema()
        actual = json.loads((root() / "design/schemas" / f"{name}.schema.json").read_bytes())
        assert actual == expected


def test_non_python_runtime_and_test_assets_have_entrypoint_references():
    paths = [*(root() / "src").rglob("*.py"), *(root() / "tests").glob("test_*.py")]
    literals = {
        node.value
        for path in paths
        if path != Path(__file__)
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assets = [
        *(root() / "tests/fixtures").iterdir(),
        *(root() / "src/fba/native").iterdir(),
        root() / "tests/native_guards.cpp",
    ]
    for asset in assets:
        assert any(value == asset.name or value.endswith("/" + asset.name) for value in literals), (
            asset
        )
    assert set(p.name for p in (root() / "scripts").iterdir() if p.is_file()) == {
        "check",
        "verify.py",
    }
    assert "scripts/check" in (root() / ".github/workflows/check.yml").read_text()
    assert "scripts/verify.py" in (root() / "scripts/check").read_text()
    assert "design/contracts.pyi" in (root() / "README.md").read_text()


def test_core_dependency_direction_and_no_io_or_mutable_globals():
    allowed = {
        "collections",
        "datetime",
        "fractions",
        "typing",
        "fba.contracts",
        "math",
        "itertools",
        "numpy",
        "numpy.typing",
        "scipy.optimize",
        "scipy.special",
        "scipy.stats",
    }
    forbidden_calls = {"open", "eval", "exec", "__import__", "print", "input"}
    for path in (root() / "src/fba/core").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module in allowed or node.module.startswith(
                    ("fba.contracts.", "fba.core.")
                ), path
            if isinstance(node, ast.Import):
                assert all(n.name in allowed for n in node.names), path
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in forbidden_calls, path
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {
                    "load",
                    "save",
                    "savez",
                    "savez_compressed",
                    "loadtxt",
                    "savetxt",
                    "fromfile",
                    "tofile",
                    "memmap",
                    "genfromtxt",
                    "ctypeslib",
                }, path
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


def test_long_functions_do_not_duplicate_eighty_percent_of_their_body():
    bodies = []
    for path in (root() / "src/fba").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                lines = ast.unparse(ast.Module(body=node.body, type_ignores=[])).splitlines()
                if len(lines) >= 10:
                    bodies.append((path.name, node.name, lines))
    for i, (path, name, lines) in enumerate(bodies):
        for other_path, other_name, other_lines in bodies[i + 1 :]:
            match = difflib.SequenceMatcher(None, lines, other_lines, autojunk=False)
            if match.quick_ratio() >= 0.8:
                assert match.ratio() < 0.8, (path, name, other_path, other_name)


def test_native_core_has_no_fixed_league_dimensions_or_io():
    import re

    source = (root() / "src/fba/native/season.cpp").read_text()
    assert not re.search(r"\b(14|200|82)\b|\b(PG|SG|SF|PF|OREB|DD)\b", source)
    assert not re.search(r"\b(fopen|fread|fwrite|socket|system|popen)\s*\(", source)
    assert "std::array" not in source
    assert "int R, int L, int I" in source


def test_native_function_complexity_is_bounded():
    functions = lizard.analyze_file(str(root() / "src/fba/native/season.cpp")).function_list
    assert functions
    assert all(f.cyclomatic_complexity <= 15 for f in functions), [
        (f.name, f.cyclomatic_complexity) for f in functions if f.cyclomatic_complexity > 15
    ]
