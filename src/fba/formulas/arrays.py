"""Executable vector equations with input/result evidence."""

from typing import NamedTuple

import numpy as np

from fba.contracts.base import DataError
from fba.contracts.formula import ArrayFormulaTrace, FormulaDefinition, NumericValue
from fba.formulas.array_catalog import ARRAY_FORMULAS as VECTOR_FORMULAS
from fba.formulas.simulation_catalog import ARRAY_FORMULAS as SIMULATION_FORMULAS
from fba.formulas.vector import Array

ARRAY_FORMULAS = (*VECTOR_FORMULAS, *SIMULATION_FORMULAS)


class ArrayCalculation(NamedTuple):
    result: Array
    formula_id: str
    inputs: tuple[tuple[str, Array], ...]

    @property
    def trace(self) -> ArrayFormulaTrace:
        # Keep immutable NumPy evidence on the search path; serialize only when
        # exposing it, without materializing Python tuples for every candidate.
        return ArrayFormulaTrace(
            formula_id=self.formula_id,
            inputs={k: numeric(v) for k, v in self.inputs},
            result=numeric(self.result),
        )


def numeric(value: Array) -> NumericValue:
    return float(value) if value.ndim == 0 else tuple(numeric(row) for row in value)


def evaluate_array(formula_id: str, **inputs: Array | float) -> ArrayCalculation:
    definition = next((f for f in ARRAY_FORMULAS if f.id == formula_id), None)
    if definition is None or set(inputs) != set(definition.input_units):
        raise DataError(f"formula.{formula_id}: unknown formula or incompatible inputs")
    arrays = {k: np.array(v, dtype=np.float64, copy=True) for k, v in inputs.items()}
    if any(not np.isfinite(v).all() for v in arrays.values()):
        raise DataError(f"formula.{formula_id}: non-finite input")
    for value in arrays.values():
        value.flags.writeable = False
    try:
        result = np.asarray(definition.implementation(arrays), dtype=np.float64)
    except DataError:
        raise
    except (ValueError, IndexError, np.linalg.LinAlgError):
        raise DataError(f"formula.{formula_id}: incompatible numeric shape or domain") from None
    if not np.isfinite(result).all():
        raise DataError(f"formula.{formula_id}: non-finite result")
    result.flags.writeable = False
    return ArrayCalculation(result, formula_id, tuple(arrays.items()))


def array_definitions() -> tuple[FormulaDefinition, ...]:
    return tuple(
        FormulaDefinition(
            id=f.id,
            name=f.name,
            latex=f.latex,
            units=f.units,
            parameters=f.parameters,
            input_units=f.input_units,
            example=evaluate_array(
                f.id, **{k: np.asarray(v, dtype=np.float64) for k, v in f.example.items()}
            ).trace,
        )
        for f in ARRAY_FORMULAS
    )
