import json
from hashlib import sha256
from pathlib import Path

import pytest
from formula_ownership_support import NumericalInventory, census

from fba.formulas.arrays import ARRAY_FORMULAS
from fba.formulas.registry import registry


def test_native_and_javascript_source_inventory_covers_every_current_file():
    root = Path(__file__).parents[1]
    inventory = json.loads((root / "design/formula-ownership.json").read_text())["source_files"]
    actual = {
        path.relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in (root / "src/fba").rglob("*")
        if path.suffix in (".js", ".cpp")
    }
    assert actual == {name: row["sha256"] for name, row in inventory.items()}
    ids = {row.id for row in (*registry(), *ARRAY_FORMULAS)}
    assert all(
        row["role"] in {"composition", "structure", "pending"}
        and row["reason"]
        and set(row["formula_ids"]) <= ids
        for row in inventory.values()
    )


def test_numerical_census_has_no_unrecorded_or_changed_sites():
    root = Path(__file__).parents[1]
    inventory = json.loads((root / "design/formula-ownership.json").read_text())
    entries = inventory["candidates"]
    assert census(root) == {key: row["signature"] for key, row in entries.items()}
    roles = {"registered", "component", "composition", "validation", "structure", "pending"}
    assert all(row["role"] in roles and row["reason"] for row in entries.values())
    # Pending is explicit remaining audit work, never proof of formula-layer acceptance.
    assert all(row["role"] != "pending" or row["owner"] == key for key, row in entries.items())


def test_registered_equations_have_exactly_one_inventory_owner_across_the_package():
    root = Path(__file__).parents[1]
    entries = json.loads((root / "design/formula-ownership.json").read_text())["candidates"]
    formulas = (*registry(), *ARRAY_FORMULAS)
    owners = {
        f"{row.implementation.__module__}.{row.implementation.__qualname__}": row.id
        for row in formulas
    }
    assert len(owners) == len(formulas) == len({row.id for row in formulas})
    assert owners == {
        key: row["owner"] for key, row in entries.items() if row["role"] == "registered"
    }
    assert all(key.startswith("fba.formulas.") for key in owners)
    assert all(row["owner"] in owners for row in entries.values() if row["role"] == "component")


def test_census_detects_body_nested_lambda_and_module_calculations_but_not_annotations():
    import ast

    visitor = NumericalInventory("fba.example")
    visitor.visit(
        ast.parse("""
limit = 2 * 3
class Example:
    window = max(1, 2)
    def operation(self, values: list[int | float]):
        data: int | float
        transform = lambda x: x / 2
        def inner():
            return sum(values)
        return values.mean() + transform(inner())
""")
    )
    assert set(visitor.candidates) == {
        "fba.example.<module>",
        "fba.example.Example.<class>",
        "fba.example.Example.operation",
        "fba.example.Example.operation<locals>.inner",
    }
    modified = NumericalInventory("fba.example")
    modified.visit(ast.parse("def operation(values):\n    return values.var()"))
    assert "fba.example.operation" in modified.candidates


@pytest.mark.parametrize("changed", ("-(wins + 0.5 * ties)", "abs(wins + 0.5 * ties)"))
def test_numerical_signature_detects_changes_to_outer_expression(changed):
    import ast

    original = NumericalInventory("fba.case")
    original.visit(ast.parse("def credit(wins, ties):\n    return wins + 0.5 * ties"))
    modified = NumericalInventory("fba.case")
    modified.visit(ast.parse(f"def credit(wins, ties):\n    return {changed}"))
    assert original.candidates.keys() == modified.candidates.keys()
    assert original.candidates != modified.candidates


def test_census_includes_standalone_numeric_negation():
    import ast

    visitor = NumericalInventory("fba.case")
    visitor.visit(ast.parse("def margin(value):\n    return -value"))
    assert set(visitor.candidates) == {"fba.case.margin"}


@pytest.mark.parametrize("function", ("minimum", "maximum", "add", "logaddexp", "count_nonzero"))
def test_census_includes_numpy_numeric_calls_without_arithmetic_operators(function):
    import ast

    visitor = NumericalInventory("fba.case")
    visitor.visit(ast.parse(f"def calculation(values):\n    return np.{function}(values, 0)"))
    assert set(visitor.candidates) == {"fba.case.calculation"}
