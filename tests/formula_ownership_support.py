"""Syntactic census for the separately reviewed numerical ownership inventory.

This finds candidates, including paths/sets/string arithmetic, not equations.
It deliberately does not infer that an unclassified candidate is safe.
"""

import ast
from hashlib import sha256
from pathlib import Path

NUMERIC_CALLS = frozenset(
    "sum fsum mean var std sqrt exp log log2 erf expit ndtr norm ppf divide multiply "
    "einsum outer cov dot matmul poisson binomial cumsum cumprod min max clip round "
    "nextafter floor ceil ptp average prod rint percentile quantile median abs absolute "
    "negative positive sign power square reciprocal where minimum maximum add logaddexp "
    "count_nonzero".split()
)
STRUCTURAL_OPERATORS = (ast.BitOr, ast.BitAnd, ast.LShift, ast.RShift)


class NumericalSites(ast.NodeVisitor):
    def __init__(self):
        self.sites = set()

    def visit_FunctionDef(self, node):
        pass  # Nested functions have their own scope and inventory entry.

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

    def visit_AnnAssign(self, node):
        if node.value is not None:
            self.visit(node.value)  # Type unions/generic shapes are not calculations.

    def visit_BinOp(self, node):
        if not isinstance(node.op, STRUCTURAL_OPERATORS):
            self.sites.add(ast.dump(node, include_attributes=False))
        self.generic_visit(node)

    visit_AugAssign = visit_BinOp

    def visit_UnaryOp(self, node):
        if isinstance(node.op, (ast.USub, ast.UAdd)):
            self.sites.add(ast.dump(node, include_attributes=False))
        self.generic_visit(node)

    def visit_Call(self, node):
        name = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
            if isinstance(node.func, ast.Attribute)
            else ""
        )
        if name in NUMERIC_CALLS:
            self.sites.add(ast.dump(node, include_attributes=False))
        self.generic_visit(node)


def signature(statements):
    visitor = NumericalSites()
    for statement in statements:
        visitor.visit(statement)
    if not visitor.sites:
        return None
    # Include the enclosing context: negation, abs(), branches and statement order
    # can change a result without changing any inner arithmetic node.
    body = ast.Module(body=list(statements), type_ignores=[])
    return sha256(ast.dump(body, include_attributes=False).encode()).hexdigest()


class NumericalInventory(ast.NodeVisitor):
    def __init__(self, module):
        self.scope = [module]
        self.candidates = {}

    def record(self, name, body):
        if (value := signature(body)) is not None:
            key = ".".join((*self.scope, name))
            assert key not in self.candidates, f"Ambiguous numerical owner: {key}"
            self.candidates[key] = value

    def visit_Module(self, node):
        self.record("<module>", node.body)
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        self.scope.append(node.name)
        self.record("<class>", node.body)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node):
        self.record(node.name, node.body)
        self.scope.append(node.name + "<locals>")
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef


def census(root: Path):
    result = {}
    for path in sorted((root / "src/fba").rglob("*.py")):
        module = path.relative_to(root / "src").with_suffix("").as_posix().replace("/", ".")
        visitor = NumericalInventory(module)
        visitor.visit(ast.parse(path.read_text()))
        result.update(visitor.candidates)
    return result
