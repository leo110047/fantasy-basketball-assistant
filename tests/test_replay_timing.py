import pytest
from test_backtest import kernel as kernel
from test_backtest import replay_fixture
from test_managed import reference_manager
from test_season import management_parameters

from fba.contracts.season import ManagementInput, ManagementPolicy
from fba.core.managed import ManagedSeason
from fba.core.season import replay


def timing_manager(kernel, effective, schedule, utilities):
    original, _, _ = reference_manager(kernel, 1)
    league = original.league.model_copy(
        update={
            "starter_slots": original.league.starter_slots[:1],
            "bench_slots": 0,
            "injury_slots": (),
            "transactions": original.league.transactions.model_copy(
                update={"effective": effective, "adds_per_period": 1}
            ),
        }
    )
    from fba.contracts.auction import AuctionPlayer

    players = tuple(
        p.model_copy(
            update={
                "game_days": tuple(original.days[d] for d in schedule[i]),
                "expected_games": float(len(schedule[i])),
                "healthy_games": float(len(schedule[i])),
                "season_games": float(len(schedule[i])),
                "return_on": None,
            }
        )
        for i, p in enumerate(original.players[: len(schedule)])
    )
    catalog = tuple(
        AuctionPlayer(
            id=p.id,
            name=p.id,
            positions=league.positions,
            positions_confirmed=True,
            active=True,
            projected_price=1.0,
            fair=1.0,
            utility=float(utilities[i]),
        )
        for i, p in enumerate(players)
    )
    return ManagedSeason(
        league,
        original.parameters.model_copy(update={"health_samples": 1}),
        ManagementInput(
            stat_ids=original.stat_ids, sampling_ids=tuple(p.id for p in players), players=players
        ),
        catalog,
        kernel,
    )


def test_public_availability_retires_stale_return_date_without_reading_future(kernel):
    from datetime import timedelta

    import numpy as np

    from fba.contracts.auction import AuctionPlayer

    original = timing_manager(kernel, "same_day", ((0, 1, 2), (0, 1, 2)), (10, 1))
    players = tuple(
        p.model_copy(update={"return_on": original.days[-1] + timedelta(days=2)})
        for p in original.players
    )
    catalog = tuple(
        AuctionPlayer(
            id=p.id,
            name=p.id,
            positions=original.league.positions,
            positions_confirmed=True,
            active=True,
            projected_price=1.0,
            fair=1.0,
            utility=10.0,
        )
        for p in players
    )
    health = np.zeros((1, original.d, original.n), dtype=bool)
    health[0, 1, 0] = True

    def observed(tape):
        return ManagedSeason(
            original.league,
            original.parameters,
            ManagementInput(stat_ids=original.stat_ids, sampling_ids=original.ids, players=players),
            catalog,
            kernel,
            tape,
        )

    manager = observed(health)
    assert manager.forecast(0, False, 1)[0] == 0
    assert manager.forecast(1, True, 1)[0] == 1
    # The old hint stays retired if a subsequent public report says unavailable again.
    assert manager.forecast(2, False, 1)[0] == 0
    assert manager.forecast(1, False, 2)[0] > manager.forecast(1, False, 2)[1]
    changed = health.copy()
    changed[0, 2:] = True
    later = observed(changed)
    for day in (0, 1):
        for status in (False, True):
            np.testing.assert_array_equal(
                manager.forecast(day, status), later.forecast(day, status)
            )


def test_available_star_is_not_streamed_out_due_to_old_return_estimate(kernel):
    from datetime import timedelta

    source, auction = replay_fixture(kernel)
    star = source.teams[0].roster[0]
    source = source.model_copy(
        update={
            "health": tuple(
                e.model_copy(update={"available": True, "status": "available"})
                for e in source.health
            ),
            "upgrades": True,
        }
    )
    auction = auction.model_copy(
        update={
            "players": tuple(
                p.model_copy(update={"utility": 1e6}) if p.id == star else p
                for p in auction.players
            ),
            "management": auction.management.model_copy(
                update={
                    "players": tuple(
                        p.model_copy(
                            update={
                                "return_on": source.decision_times[-1].date() + timedelta(days=1)
                            }
                        )
                        if p.id == star
                        else p
                        for p in auction.management.players
                    ),
                }
            ),
        }
    )
    result = replay(source, auction, "0" * 64, kernel)
    index = result.player_ids.index(star)
    assert not [e for e in result.events if e.team == 0 and e.dropped == index]


def test_replay_rejects_missing_utility_in_rosters_and_free_agents(kernel):
    import pytest

    from fba.contracts.base import DataError

    source, auction = replay_fixture(kernel)
    for player_id in (source.teams[0].roster[0], source.free_agents[0]):
        changed = auction.model_copy(
            update={
                "players": tuple(
                    p.model_copy(update={"utility": None}) if p.id == player_id else p
                    for p in auction.players
                ),
            }
        )
        with pytest.raises(DataError, match="projection"):
            replay(source, changed, "0" * 64, kernel)


@pytest.mark.parametrize("effective", ["same_day", "next_day"])
@pytest.mark.parametrize("upgrades", [False, True])
def test_acquisition_ignores_games_before_effective_date(kernel, effective, upgrades):
    manager = timing_manager(kernel, effective, ((1,), (0,)), (1, 10))
    policy = ManagementPolicy(streaming_slots=(1,), reserve_adds=0, upgrades=upgrades)
    result = kernel.run(
        manager.arrays(),
        ((0,),),
        (1,),
        manager.tactics(policy, management_parameters(manager, 0)),
        True,
    )
    trades = [e for e in result.events if e.kind in ("stream", "upgrade")]
    assert bool(trades) == (effective == "same_day")
    assert result.counts.sum() == 1


def test_weekly_locked_actuals_do_not_depend_on_later_health_report(kernel):
    source, auction = replay_fixture(kernel)
    league = source.config.league.model_copy(
        update={
            "lineup": source.config.league.lineup.model_copy(
                update={
                    "lock_mode": "weekly",
                    "lock_at": "period_start",
                }
            ),
            "injury_slots": (),
            "transactions": source.config.league.transactions.model_copy(
                update={"adds_per_period": 0}
            ),
        }
    )
    model = source.config.model.model_copy(
        update={
            "management": source.config.model.management.model_copy(update={"reserve_adds": 0}),
        }
    )
    config = source.config.model_copy(update={"league": league, "model": model})
    source = source.model_copy(
        update={
            "config": config,
            "health": tuple(
                e.model_copy(update={"available": True, "status": "available"})
                for e in source.health
            ),
        }
    )
    auction = auction.model_copy(update={"config": config})
    baseline = replay(source, auction, "0" * 64, kernel)
    later = next(e for e in baseline.events if e.kind == "lineup" and e.day > 0 and e.started)
    player = baseline.player_ids[later.started[0]]
    changed = source.model_copy(
        update={
            "health": tuple(
                e.model_copy(update={"available": False, "status": model.fit.unavailable_status})
                if e.player_id == player and e.published_at.date() == baseline.days[later.day]
                else e
                for e in source.health
            )
        }
    )
    actual = replay(changed, auction, "0" * 64, kernel)
    assert actual.weekly_boxes == baseline.weekly_boxes
    assert actual.outcomes == baseline.outcomes


@pytest.mark.parametrize("upgrades", [False, True])
def test_delayed_candidate_limit_orders_only_usable_games(kernel, upgrades):
    manager = timing_manager(kernel, "next_day", ((1,), (0,), (1,), (1,)), (1, 10, 2, 2))
    policy = ManagementPolicy(streaming_slots=(1,), reserve_adds=0, upgrades=upgrades)
    parameters = management_parameters(manager, 0).model_copy(update={"candidate_limit": 1})
    result = kernel.run(
        manager.arrays(), ((0,),), (1, 2, 3), manager.tactics(policy, parameters), True
    )
    trades = [e for e in result.events if e.kind in ("stream", "upgrade")]
    assert [(e.day, e.dropped, e.added) for e in trades] == [(0, 0, 2)]
    assert result.counts[0, 0, 0].tolist() == [0, 0, 1, 0]


@pytest.mark.parametrize("upgrades", [False, True])
def test_delayed_add_does_not_discard_the_held_players_today_value(kernel, upgrades):
    manager = timing_manager(kernel, "next_day", ((0,), (1,)), (10, 1))
    policy = ManagementPolicy(streaming_slots=(1,), reserve_adds=0, upgrades=upgrades)
    result = kernel.run(
        manager.arrays(),
        ((0,),),
        (1,),
        manager.tactics(policy, management_parameters(manager, 0)),
        True,
    )
    assert not any(e.kind in ("stream", "upgrade") for e in result.events)
    assert result.counts[0, 0, 0].tolist() == [1, 0]


@pytest.mark.parametrize("effective", ["same_day", "next_day"])
def test_weekly_add_requires_a_remaining_eligible_lineup_lock(kernel, effective):
    schedule, utilities = (
        (((0,), (1,)), (10, 1)) if effective == "same_day" else (((1,), (0, 1)), (1, 10))
    )
    manager = timing_manager(kernel, effective, schedule, utilities)
    manager.league = manager.league.model_copy(
        update={
            "lineup": manager.league.lineup.model_copy(
                update={"lock_mode": "weekly", "lock_at": "period_start"}
            ),
        }
    )
    policy = ManagementPolicy(streaming_slots=(1,), reserve_adds=0, upgrades=False)
    result = kernel.run(
        manager.arrays(),
        ((0,),),
        (1,),
        manager.tactics(policy, management_parameters(manager, 0)),
        True,
    )
    assert not any(e.kind == "stream" for e in result.events)
    assert result.counts[0, 0, 0].tolist() == [1, 0]
