"""Bounded reuse of the pure solver's immutable eligibility results."""

from functools import lru_cache

import numpy as np
from numpy.typing import NDArray

from fba.core.lineups import legal_subsets


@lru_cache(maxsize=64)
def cached_subsets(
    eligibility: tuple[tuple[bool, ...], ...], slots: int
) -> tuple[tuple[tuple[int, ...], tuple[int | None, ...]], ...]:
    return legal_subsets(eligibility, slots)


def ordered_row_sums(
    rows: tuple[tuple[str, ...], ...],
    values: dict[str, NDArray[np.float64]],
    shape: tuple[int, ...],
    fixed: tuple[str, ...] = (),
) -> NDArray[np.float64]:
    """Reuse ordered subset prefixes; keep the original floating point additions."""
    if len(rows) == 1:
        return np.stack([sum((values[p] for p in (*fixed, *rows[0])), start=np.zeros(shape))])
    totals: dict[tuple[str, ...], NDArray[np.float64]] = {
        (): sum((values[p] for p in fixed), start=np.zeros(shape))
    }

    def ordered(row: tuple[str, ...]) -> NDArray[np.float64]:
        if row not in totals:
            totals[row] = ordered(row[:-1]) + values[row[-1]]
        return totals[row]

    return np.stack([ordered(row) for row in rows])
