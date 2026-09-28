from . import qmc
import numpy as np
from numpy.typing import NDArray
class _Norm:
    def ppf(self, q: NDArray[np.float64]) -> NDArray[np.float64]: ...
norm: _Norm
