"""Evaluate registered formulas and expose their shared UI definitions."""

from fba.contracts.base import DataError
from fba.contracts.formula import FormulaDefinition, FormulaTrace, ScalarFormula
from fba.formulas.arrays import array_definitions
from fba.formulas.scalar_catalog import FORMULAS


def registry() -> tuple[ScalarFormula, ...]:
    return FORMULAS


def evaluate(formula_id: str, **inputs: float | tuple[float, ...]) -> FormulaTrace:
    definition = next((f for f in registry() if f.id == formula_id), None)
    if definition is None:
        raise DataError(f"formula.{formula_id}: unknown formula")
    if set(inputs) != set(definition.input_units):
        raise DataError(f"formula.{formula_id}: input keys do not match the definition")
    try:
        value = definition.implementation(inputs)
    except DataError:
        raise
    except (KeyError, ZeroDivisionError, ValueError, OverflowError) as exc:
        raise DataError(f"formula.{formula_id}: invalid inputs ({type(exc).__name__})") from exc
    return FormulaTrace(formula_id=formula_id, inputs=inputs, result=value)


def definitions() -> tuple[FormulaDefinition, ...]:
    return (
        tuple(
            FormulaDefinition(
                id=f.id,
                name=f.name,
                latex=f.latex,
                units=f.units,
                parameters=f.parameters,
                input_units=f.input_units,
                example=FormulaTrace(
                    formula_id=f.id, inputs=f.example, result=f.implementation(f.example)
                ),
            )
            for f in registry()
        )
        + array_definitions()
    )
