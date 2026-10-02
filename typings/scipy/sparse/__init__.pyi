from collections.abc import Sequence
from typing import Any

import numpy as np
from numpy.typing import NDArray

class csr_matrix:
    data: NDArray[np.int32]
    indptr: NDArray[np.int32]
    indices: NDArray[np.int32]
    def __init__(
        self,
        arg1: tuple[Sequence[int], tuple[Sequence[int], Sequence[int]]],
        shape: tuple[int, int],
        dtype: Any,
    ) -> None: ...
    def copy(self) -> csr_matrix: ...
    def __sub__(self, other: csr_matrix) -> csr_matrix: ...
