"""Update conditional minutes and availability from the prior and observed games."""

from fba.contracts.base import DataError
from fba.contracts.formula import FormulaTrace
from fba.contracts.inseason import PlayerPrior
from fba.formulas.registry import evaluate


def estimate_role(
    prior: PlayerPrior, minutes: tuple[float, ...], k: float, half_life: float
) -> tuple[FormulaTrace, FormulaTrace]:
    if prior.appearance_probability is None:
        raise DataError("prior: 舊預測缺少出賽場數；請重新載入已鎖定的賽季前預測")
    conditional = evaluate(
        "minutes",
        k=k,
        prior=prior.minutes,
        minutes=tuple(m for m in minutes if m > 0),
        half_life=half_life,
    )
    appearance = evaluate(
        "blend",
        k=k,
        prior=prior.appearance_probability,
        total=float(sum(m > 0 for m in minutes)),
        sample=float(len(minutes)),
    )
    return conditional, appearance
