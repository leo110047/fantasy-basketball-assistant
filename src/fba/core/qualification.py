"""Discrete remaining-results feasibility; no Monte Carlo elimination claims."""

from fractions import Fraction
from math import lcm, sqrt

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp


def qualification_possible(
    points: tuple[float, ...],
    tie_order: tuple[int, ...],
    mine: int,
    places: int,
    fixtures: tuple[tuple[int, int], ...],
    selected: int,
    ties: tuple[float, ...],
    win: bool,
    seconds: float,
    tolerance: float,
) -> bool | None:
    """Can any remaining result put mine in the top places after win/loss?

    One scoring unit represents a weekly result or one configured category.
    Our other fixtures can all be won: doing so improves our points and never
    increases a rival's points. Other results remain jointly constrained.
    None means the solver or score precision could not certify an answer.
    """
    if tolerance <= 0 or tolerance > 1:
        return None
    precision = int(1 / sqrt(tolerance))
    numbers = tuple(Fraction(str(v)).limit_denominator(precision) for v in (*points, *ties))
    if any(abs(float(n) - v) > tolerance for n, v in zip(numbers, (*points, *ties), strict=True)):
        return None
    scale = lcm(*(n.denominator for n in numbers))
    if scale > precision or seconds <= 0:
        return None
    units = len(ties)
    outcomes = 2 * units * len(fixtures)
    rivals = tuple(t for t in range(len(points)) if t != mine)
    size = outcomes + len(rivals)
    scores = np.zeros((len(points), size))
    constant = np.array([float(n * scale) for n in numbers[: len(points)]])
    lower, upper = np.zeros(size), np.ones(size)
    rows: list[np.ndarray[tuple[int, ...], np.dtype[np.float64]]] = []
    lows: list[float] = []
    highs: list[float] = []
    current = np.zeros(size)
    for index, (home, away) in enumerate(fixtures):
        for unit, tie in enumerate(numbers[len(points) :]):
            w, t = 2 * (index * units + unit), 2 * (index * units + unit) + 1
            constant[away] += scale
            scores[home, w], scores[away, w] = scale, -scale
            scores[home, t], scores[away, t] = float(tie * scale), float((tie - 1) * scale)
            row = np.zeros(size)
            row[w] = row[t] = 1.0
            rows.append(row)
            lows.append(0.0)
            highs.append(1.0)
            if mine in (home, away) and index != selected:
                lower[w] = upper[w] = float(mine == home)
                upper[t] = 0.0
            if index == selected:
                sign = 1.0 if mine == home else -1.0
                current[w], current[t] = 2 * sign, sign
    home, _ = fixtures[selected]
    offset = units * (1.0 if mine == home else -1.0)
    rows.append(current)
    lows.append(offset + 1 if win else -np.inf)
    highs.append(np.inf if win else offset - 1)
    # A binary permits each rival to finish above us; at most places-1 may do so.
    bound = float(max(constant) - min(constant) + len(fixtures) * units * scale + scale)
    for i, rival in enumerate(rivals):
        row = scores[rival] - scores[mine]
        row[outcomes + i] = -bound
        rows.append(row)
        lows.append(-np.inf)
        highs.append(constant[mine] - constant[rival] - int(tie_order[rival] < tie_order[mine]))
    row = np.zeros(size)
    row[outcomes:] = 1.0
    rows.append(row)
    lows.append(0.0)
    highs.append(float(places - 1))
    for i in np.flatnonzero((lower != 0) | (upper != 1)):
        row = np.zeros(size)
        row[i] = 1.0
        rows.append(row)
        lows.append(float(lower[i]))
        highs.append(float(upper[i]))
    matrix = np.stack(rows)
    result = milp(
        np.zeros(size),
        integrality=np.ones(size),
        bounds=Bounds(0.0, 1.0),
        constraints=LinearConstraint(matrix, np.array(lows), np.array(highs)),
        options={"time_limit": seconds, "mip_rel_gap": 0.0},
    )
    if result.status == 2:
        return False
    if result.x is None:
        return None
    witness = np.rint(np.asarray(result.x, dtype=float))
    values = matrix @ witness
    if (
        np.any(witness < lower)
        or np.any(witness > upper)
        or np.any(values < np.array(lows) - tolerance)
        or np.any(values > np.array(highs) + tolerance)
    ):
        return None
    return True
