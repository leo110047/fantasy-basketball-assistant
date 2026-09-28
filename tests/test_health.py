from datetime import date, timedelta

import numpy as np
import pytest
from test_auction import config, player

from fba.contracts.base import DataError
from fba.contracts.config import HealthParameters
from fba.contracts.season import ManagedPlayer, ManagementInput
from fba.core.health import healthy_games
from fba.core.managed import ManagedSeason


def parameters(share):
    return HealthParameters(injury_share=share, evidence=config().model.fit.evidence)


@pytest.mark.parametrize("share", [0.0, 0.5, 1.0])
def test_injury_split_preserves_expected_production_and_noninjury_loss(share):
    c = config()
    days = tuple(date(2026, 10, 20) + timedelta(days=i) for i in range(10))
    health = healthy_games(6, 10, None, days, parameters(share))
    p = ManagedPlayer(
        id="000",
        expected_games=6,
        healthy_games=health,
        season_games=10,
        return_on=None,
        game_days=days,
        means=(20.0,),
        covariance=((4.0,),),
    )

    class UnusedKernel:
        def __call__(self, *args):
            raise AssertionError("not used")

    m = ManagedSeason(
        c.league,
        c.model.fit,
        ManagementInput(stat_ids=("PTS",), sampling_ids=("000",), players=(p,)),
        (player(0),),
        UnusedKernel(),
    )
    assert m.raw[0, 0] * m.availability[0] * 10 == pytest.approx(120)
    assert 10 - health == pytest.approx((10 - 6) * share)
    assert health - 6 == pytest.approx((10 - 6) * (1 - share))
    if share == 0:
        assert m.health.all()
    else:
        assert not m.health.all() and m.health.any()
    assert np.isfinite(m.cov).all()


@pytest.mark.parametrize("share", [0.0, 0.5, 1.0])
def test_known_return_absence_is_not_reclassified_as_noninjury(share):
    days = tuple(date(2026, 10, 20) + timedelta(days=i) for i in range(10))
    # Four definite absent games, then six opportunities and four expected appearances.
    assert healthy_games(4, 10, days[4], days, parameters(share)) == pytest.approx(6 - 2 * share)
    assert healthy_games(0, 10, days[-1] + timedelta(days=1), days, parameters(share)) == 0
    with pytest.raises(DataError, match="eligible schedule"):
        healthy_games(7, 10, days[4], days, parameters(share))


def test_injury_share_is_required_finite_and_bounded():
    for value in (-0.1, 1.1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            parameters(value)


def test_role_opportunity_loss_does_not_increase_injury_risk():
    from types import SimpleNamespace

    from test_auction import player

    from fba.adapters.auction import prepare_management
    from fba.contracts.projection import Projected, RoleProjected
    from fba.contracts.season import RoleManagedPlayer

    dates = tuple(date(2026, 10, 20) + timedelta(days=i) for i in range(10))
    rows = (
        RoleProjected(
            id="000",
            expected_games=3,
            unconstrained_games=6,
            minutes=30,
            stats=(20.0,),
            covariance=((4.0,),),
        ),
        Projected(id="001", expected_games=6, minutes=30, stats=(20.0,), covariance=((4.0,),)),
    )
    c = config()
    c = c.model_copy(
        update={
            "model": c.model.model_copy(
                update={
                    "projection": c.model.projection.model_copy(
                        update={"stat_ids": (), "threshold_stat": "PTS"}
                    )
                }
            )
        }
    )
    inputs = SimpleNamespace(
        config=c,
        players=tuple(SimpleNamespace(id=f"{i:03}", team_id="A", return_on=None) for i in range(2)),
        teams=(SimpleNamespace(id="A", full_season_games=10, dates=dates),),
    )
    result = SimpleNamespace(projections=rows)
    for share in (0, 0.5, 1):
        managed = prepare_management(inputs, result, (player(0), player(1)), parameters(share))
        a, b = managed.players
        assert isinstance(a, RoleManagedPlayer) and isinstance(b, RoleManagedPlayer)
        assert a.healthy_games == b.healthy_games == 10 - 4 * share
        assert a.expected_games == 3 and b.expected_games == 6
        assert a.unconstrained_games == b.unconstrained_games == 6


def test_full_injury_share_preserves_fractional_expected_games_exactly():
    expected = 29.59667988
    assert healthy_games(expected, 82, None, (), parameters(1)) == expected
