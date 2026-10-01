"""Translate the registered management equation for the existing C++ kernel.

This bridge accepts only the expression syntax used by management_gain. It
never evaluates untrusted source or stores a second copy of the equation.
"""

import ast
import inspect
from collections.abc import Callable

from fba.contracts.auction import SolverError
from fba.formulas.scalar import ScalarInputs, management_gain, product


def management_formula() -> str:
    return native_function(
        management_gain,
        "management_gain",
        (
            "acquired_short",
            "held_short",
            "acquired_long",
            "held_long",
            "opportunity_cost",
            "longer",
        ),
    ) + native_function(product, "season_priority", ("gain", "probability"))


def native_function(
    implementation: Callable[[ScalarInputs], float], name: str, arguments: tuple[str, ...]
) -> str:
    function = ast.parse(inspect.getsource(implementation)).body[0]
    if not isinstance(function, ast.FunctionDef) or len(function.body) != 1:
        raise SolverError("management.formula: expected one pure return expression")
    statement = function.body[0]
    if not isinstance(statement, ast.Return) or statement.value is None:
        raise SolverError("management.formula: expected a return value")
    declaration = ", ".join(f"double {name}" for name in arguments)
    expression = native_expression(statement.value, arguments)
    return f"static inline double {name}({declaration}) {{ return {expression}; }}\n"


def native_expression(node: ast.AST, arguments: tuple[str, ...]) -> str:
    if isinstance(node, ast.Constant) and type(node.value) is float:
        return repr(node.value)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Sub, ast.Mult)):
        operator = "-" if isinstance(node.op, ast.Sub) else "*"
        return (
            f"({native_expression(node.left, arguments)} {operator} "
            f"{native_expression(node.right, arguments)})"
        )
    if isinstance(node, ast.IfExp):
        return (
            f"({native_expression(node.test, arguments)} ? "
            f"{native_expression(node.body, arguments)} : "
            f"{native_expression(node.orelse, arguments)})"
        )
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
        if node.func.id == "number" and len(node.args) == 2:
            values, key = node.args
            if (
                isinstance(values, ast.Name)
                and values.id == "values"
                and isinstance(key, ast.Constant)
                and key.value in arguments
            ):
                return str(key.value)
        if node.func.id == "max" and len(node.args) == 2:
            return "std::max(" + ", ".join(native_expression(a, arguments) for a in node.args) + ")"
    raise SolverError("management.formula: unsupported equation syntax")
