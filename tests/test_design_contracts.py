import ast
import importlib
from pathlib import Path


def test_design_reexports_current_contracts_without_shadow_schemas():
    path = Path(__file__).parents[1] / "design/contracts.pyi"
    tree = ast.parse(path.read_text())
    runtime = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module.startswith("fba.contracts."):
            module = importlib.import_module(node.module)
            for name in node.names:
                assert name.asname == name.name
                runtime[name.name] = getattr(module, name.name)
    assert {"DraftState", "Sale", "Cap", "AuctionResult", "Provenance"} <= runtime.keys()
    declarations = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
    assert not declarations.intersection(runtime)
    assert {"WeeklyAdviceInput", "WeeklyAdvice", "SeasonState", "KnownAtDay"} <= declarations
    assert {node.name for node in tree.body if isinstance(node, ast.FunctionDef)} == {"advise_week"}
