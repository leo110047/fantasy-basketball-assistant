"""Narrow SciPy public scalar quadrature boundary; full_output reports nonconvergence."""
from collections.abc import Callable
from typing import Literal

def quad(func: Callable[[float], float], a: float, b: float, *, full_output: Literal[1], epsabs: float, epsrel: float) -> tuple[float, float, object] | tuple[float, float, object, str]: ...
