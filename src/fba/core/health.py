from datetime import date

from fba.contracts.base import DataError
from fba.contracts.config import HealthParameters


def healthy_games(
    expected: float,
    full: float,
    return_on: date | None,
    dates: tuple[date, ...],
    parameters: HealthParameters,
) -> float:
    """Separate known absence, assumed injury risk and healthy nonparticipation."""
    eligible = (
        float(sum(d >= return_on for d in dates))
        if return_on is not None and dates and return_on > dates[0]
        else full
    )
    if not 0 <= expected <= eligible <= full:
        raise DataError("management.health: expected games exceed eligible schedule")
    return min(eligible, max(expected, eligible - (eligible - expected) * parameters.injury_share))
