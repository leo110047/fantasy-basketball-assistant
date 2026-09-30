"""Narrow SciPy 1.18.1 public MILP boundary; verified against upstream API."""
from typing import TypedDict
from collections.abc import Callable
import numpy as np
from numpy.typing import NDArray

class Bounds:
    def __init__(self, lb: float, ub: float) -> None: ...

class LinearConstraint:
    def __init__(self, A: NDArray[np.float64], lb: NDArray[np.float64], ub: NDArray[np.float64]) -> None: ...

class MilpOptions(TypedDict):
    time_limit: float
    mip_rel_gap: float

class OptimizeResult:
    status: int
    x: NDArray[np.float64] | None

def milp(c: NDArray[np.float64], *, integrality: NDArray[np.float64], bounds: Bounds, constraints: LinearConstraint, options: MilpOptions) -> OptimizeResult: ...

class MinimizeResult:
    success: bool
    x: NDArray[np.float64]
    message: str

def minimize(fun: Callable[[NDArray[np.float64]], float], x0: NDArray[np.float64], *, method: str, tol: float) -> MinimizeResult: ...
