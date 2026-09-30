"""Shared, replayable mathematical results for both applications."""

from collections.abc import Callable
from typing import NamedTuple

from fba.contracts.base import Finite, Record, Text

type NumericValue = Finite | tuple[NumericValue, ...]


class FormulaTrace(Record):
    formula_id: Text
    inputs: dict[str, Finite | tuple[Finite, ...]]
    result: Finite


class ArrayFormulaTrace(Record):
    formula_id: Text
    inputs: dict[str, NumericValue]
    result: NumericValue


class FormulaDefinition(Record):
    id: Text
    name: Text
    latex: Text
    units: Text
    parameters: tuple[Text, ...]
    input_units: dict[Text, Text]
    example: FormulaTrace | ArrayFormulaTrace


type ScalarInputs = dict[str, float | tuple[float, ...]]


class ScalarFormula(NamedTuple):
    id: str
    name: str
    latex: str
    units: str
    parameters: tuple[str, ...]
    implementation: Callable[[ScalarInputs], float]
    example: ScalarInputs
    input_units: dict[str, str]
