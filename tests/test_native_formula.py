import ast
import ctypes
import subprocess

import pytest

import fba.adapters.native as native
from fba.adapters.native_formula import management_formula, native_expression
from fba.contracts.auction import SolverError
from fba.formulas.registry import evaluate


@pytest.fixture
def compiled_gain(tmp_path):
    (tmp_path / "management-formula.h").write_text(management_formula())
    (tmp_path / "gain.cpp").write_text(
        '#include <algorithm>\n#include "management-formula.h"\n'
        'extern "C" double gain(double a, double b, double c, double d, double e, double f) '
        "{ return management_gain(a,b,c,d,e,f); }\n"
        'extern "C" double priority(double a, double b) { return season_priority(a,b); }\n'
    )
    subprocess.run(
        [
            native.compiler_path(),
            "-std=c++17",
            "-O2",
            "-ffp-contract=off",
            "-shared",
            "-fPIC",
            "gain.cpp",
            "-o",
            "gain.so",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        timeout=60,
    )
    library = ctypes.CDLL(str(tmp_path / "gain.so"))
    library.gain.argtypes = [ctypes.c_double] * 6
    library.gain.restype = ctypes.c_double
    library.priority.argtypes = [ctypes.c_double] * 2
    library.priority.restype = ctypes.c_double
    return library


@pytest.mark.parametrize(
    "values,expected",
    [
        ((8, 3, 5, 9, 0.5, 0), 3),
        ((8, 3, 10, 9, 0.5, 0), 5),
        ((8, 3, 5, 9, 0.5, 1), -4),
        ((8, 3, 5, 9, 0, 0), 5),
        ((3, 8, 5, 9, 0.5, 0), -7),
    ],
)
def test_generated_native_equation_matches_independent_answers_and_trace(
    compiled_gain, values, expected
):
    keys = (
        "acquired_short",
        "held_short",
        "acquired_long",
        "held_long",
        "opportunity_cost",
        "longer",
    )
    trace = evaluate("management_gain", **dict(zip(keys, values, strict=True)))
    assert trace.result == expected == compiled_gain.gain(*values)
    assert evaluate(trace.formula_id, **trace.inputs) == trace


def test_native_weekly_priority_uses_registered_product(compiled_gain):
    assert compiled_gain.priority(2.5, 4) == 10
    assert evaluate("product", gain=2.5, probability=4.0).result == 10


def test_native_bridge_rejects_unrecognized_expression_instead_of_guessing():
    with pytest.raises(SolverError, match="unsupported equation syntax"):
        native_expression(ast.parse("dangerous()", mode="eval").body, ())


def test_native_artifact_binds_generated_equation_before_worker_load(monkeypatch):
    parent = native.NativeKernel()
    try:
        original = management_formula()
        monkeypatch.setattr(native, "management_formula", lambda: original + "// changed\n")
        with pytest.raises(SolverError, match="native source or compiled bytes changed"):
            native.NativeKernel(parent.artifact)
    finally:
        parent.close()
