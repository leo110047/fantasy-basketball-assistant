import ctypes
import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import numpy as np
import pytest
from test_auction import config

from fba.adapters.native import NativeKernel
from fba.auction.managed import ManagedSeason
from fba.contracts.auction import AuctionPlayer, SolverError
from fba.contracts.base import DataError
from fba.contracts.season import ManagedPlayer, ManagementInput, SeasonArrays


@pytest.fixture(scope="module")
def kernel():
    native = NativeKernel()
    yield native
    native.close()


def reference_manager(kernel, case_index=3):
    root = Path(__file__).parent / "fixtures"
    spec = json.loads((root / "managed-reference.json").read_text())
    spec["case"] = spec["cases"][case_index]
    data = np.load(root / "managed-reference.npz", allow_pickle=False)
    c = config()
    parameters = c.model.fit.model_copy(
        update={"health_samples": 4, "health_seed": 92000 + case_index}
    )
    players = []
    managed = []
    for i, p in enumerate(spec["players"]):
        positions = tuple(
            pos for bit, pos in enumerate(c.league.positions) if spec["masks"][i] & 1 << bit
        )
        players.append(
            AuctionPlayer(
                id=p["id"],
                name=p["id"],
                positions=positions,
                positions_confirmed=True,
                active=True,
                projected_price=1.0,
                fair=1.0,
                utility=float(data["priority"][i]),
            )
        )
        managed.append(
            ManagedPlayer(
                id=p["id"],
                expected_games=float(p["gp"]),
                healthy_games=float(p["gp"]),
                season_games=82.0,
                return_on=date.fromisoformat(p["injury_note"][10:20])
                if p["injury_note"].startswith("out until ")
                else None,
                game_days=tuple(date.fromisoformat(d) for d in spec["schedule"][p["nba_team"]]),
                means=tuple(data["raw"][i]),
                covariance=tuple(tuple(row) for row in data["raw_cov"][i]),
            )
        )
    inputs = ManagementInput(
        stat_ids=(*c.model.projection.stat_ids, c.model.projection.threshold_stat),
        sampling_ids=tuple(p.id for p in players),
        players=tuple(managed),
    )
    manager = ManagedSeason(c.league, parameters, inputs, tuple(players), kernel)
    manager.health = data[f"health_{case_index}"].copy()
    # Recompute the public-status rankings from exactly this frozen health tape.
    forecasts = np.array(
        [[manager.forecast(d, status) for status in (False, True)] for d in range(manager.d)]
    )
    manager.orders = np.argsort(
        -np.where(manager.health, forecasts[None, :, 1, :], forecasts[None, :, 0, :])
        * manager.value,
        axis=-1,
        kind="stable",
    ).astype(np.int32)
    manager.set_pool(tuple(spec["case"]["pool"]))
    return manager, spec, data


def test_captured_reference_managed_moments_and_joint_covariance(kernel):
    manager, spec, data = reference_manager(kernel)
    roster = tuple(spec["case"]["rosters"][0])
    result = manager.project(roster)
    np.testing.assert_allclose(result.mean, data["3_mean"][0], rtol=1e-12, atol=1e-10)
    np.testing.assert_allclose(result.covariance, data["3_cov"][0], rtol=1e-12, atol=1e-10)
    np.testing.assert_allclose(result.boxes, data["3_boxes"][:, 0], rtol=1e-12, atol=1e-10)


def test_native_lineups_match_shared_python_matcher(kernel):
    manager, _, _ = reference_manager(kernel)
    arrays = manager.arrays()
    arrays = replace(
        arrays, health=np.ones_like(arrays.health), il_eligible=np.zeros_like(arrays.il_eligible)
    )
    roster = tuple(range(manager.league.bench_slots + len(manager.league.starter_slots)))
    actual = kernel(arrays, (roster,), ())
    expected = np.zeros_like(actual)
    for d in range(manager.d):
        for p in manager.lineup(tuple(p for p in roster if manager.games[d, p])):
            expected[:, 0, manager.week[d], p] += 1
    np.testing.assert_array_equal(actual, expected)


def test_native_future_health_cannot_change_completed_week(kernel):
    manager, spec, _ = reference_manager(kernel)
    a = manager.arrays()
    rosters = tuple(tuple(r) for r in spec["case"]["rosters"])
    baseline = kernel(a, rosters, manager.pool)
    future = a.health.copy()
    future[:, a.weeks > 0] = 1 - future[:, a.weeks > 0]
    changed = kernel(replace(a, health=future), rosters, manager.pool)
    np.testing.assert_array_equal(baseline[:, :, 0], changed[:, :, 0])


def test_il_eligibility_and_native_input_rejection(kernel):
    manager, spec, _ = reference_manager(kernel)
    a = manager.arrays()
    roster = tuple(spec["case"]["rosters"][0])
    health = np.zeros_like(a.health)
    health[:, :, list(manager.pool)] = 1
    eligible = replace(a, health=health)
    available = kernel(eligible, (roster,), manager.pool)
    refused = kernel(
        replace(eligible, il_eligible=np.zeros_like(a.il_eligible)), (roster,), manager.pool
    )
    assert available.sum() > 0 and refused.sum() == 0
    with pytest.raises(DataError, match="ownership"):
        kernel(a, (roster, roster), manager.pool)
    with pytest.raises(DataError, match="dtype"):
        kernel(replace(a, health=a.health.astype(float)), (roster,), manager.pool)
    orders = a.orders.copy()
    orders[:, :, 0] = -1
    with pytest.raises(DataError, match="orders"):
        kernel(replace(a, orders=orders), (roster,), manager.pool)


def small_arrays():
    return SeasonArrays(
        health=np.array([[[0, 1, 1], [0, 1, 1], [1, 1, 1]]], dtype=np.uint8),
        games=np.ones((3, 3), dtype=np.uint8),
        weeks=np.zeros(3, dtype=np.int32),
        periods=np.zeros(3, dtype=np.int32),
        masks=np.ones(3, dtype=np.uint64),
        slots=np.ones(1, dtype=np.uint64),
        priority=np.array([10.0, 2.0, 1.0]),
        value=np.array([10.0, 2.0, 1.0]),
        orders=np.tile(np.arange(3, dtype=np.int32), (1, 3, 1)),
        il_eligible=np.ones((3, 3, 1), dtype=np.uint8),
        lock_days=np.array([1, 0, 0], dtype=np.uint8),
        waiver_days=1,
        add_limit=1,
        next_day=False,
        weekly_lock=False,
        roster_capacity=1,
        week_count=1,
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("add_limit", 2**32),
        ("waiver_days", 2**31 - 1),
        ("week_count", 2**32),
        ("roster_capacity", 2**32),
        ("add_limit", 1.5),
        ("waiver_days", True),
    ],
)
def test_native_rejects_unrepresentable_integer_rules_before_calling_abi(
    kernel, monkeypatch, field, value
):
    def forbidden(*_args):
        pytest.fail("invalid integer reached native ABI")

    monkeypatch.setattr(kernel, "function", forbidden)
    with pytest.raises(DataError, match="native.*integer|integer.*native"):
        kernel(replace(small_arrays(), **{field: value}), ((0,),), (1, 2))


def test_native_accepts_largest_safe_waiver_delay_and_add_limit(kernel):
    a = replace(small_arrays(), waiver_days=2**31 - 1 - 3, add_limit=2**31 - 1)
    assert kernel(a, ((0,),), (1, 2)).ravel().tolist() == [1.0, 2.0, 0.0]


def test_weekly_lineup_uses_remaining_week_games_and_excludes_zero_game_players(kernel):
    a = replace(
        small_arrays(),
        health=np.ones((1, 3, 3), dtype=np.uint8),
        games=np.array([[0, 1, 1], [0, 0, 1], [0, 0, 1]], dtype=np.uint8),
        priority=np.array([100.0, 3.0, 2.0]),
        il_eligible=np.zeros((3, 3, 0), dtype=np.uint8),
        roster_capacity=3,
        weekly_lock=True,
    )
    result = kernel.run(a, ((0, 1, 2),), (), None, True)
    assert result.counts.ravel().tolist() == [0.0, 0.0, 3.0]
    assert all(e.started == (2,) for e in result.events if e.kind == "lineup")
    daily = kernel(replace(a, weekly_lock=False), ((0, 1, 2),), ())
    assert daily.ravel().tolist() == [0.0, 1.0, 2.0]


def test_forecast_is_independent_of_platform_power_last_bit(kernel, monkeypatch):
    manager, _, _ = reference_manager(kernel)
    before = np.array([manager.forecast(d, False) for d in range(manager.d)])
    power = np.power

    def last_bit(*args, **kwargs):
        return np.nextafter(power(*args, **kwargs), np.inf)

    monkeypatch.setattr(np, "power", last_bit)
    after = np.array([manager.forecast(d, False) for d in range(manager.d)])
    np.testing.assert_array_equal(before, after)


def test_injury_return_acquisition_delay_and_weekly_locked_seats(kernel):
    a = small_arrays()
    assert kernel(a, ((0,),), (1, 2)).ravel().tolist() == [1.0, 2.0, 0.0]
    delayed = replace(a, next_day=True)
    assert kernel(delayed, ((0,),), (1, 2)).ravel().tolist() == [1.0, 1.0, 0.0]
    weekly = replace(a, weekly_lock=True)
    assert kernel(weekly, ((0,),), (1, 2)).ravel().tolist() == [0.0, 2.0, 0.0]
    kept = replace(a, value=np.array([1.0, 10.0, 2.0]))
    assert kernel(kept, ((0,),), (1, 2)).ravel().tolist() == [0.0, 3.0, 0.0]
    refused = replace(a, il_eligible=np.zeros_like(a.il_eligible))
    assert kernel(refused, ((0,),), (1, 2)).ravel().tolist() == [1.0, 0.0, 0.0]
    limited = replace(a, add_limit=0)
    assert kernel(limited, ((0,),), (1, 2)).ravel().tolist() == [1.0, 0.0, 0.0]


def test_injured_replacement_chain_releases_a_seat_and_shared_pool_is_unique(kernel):
    a = small_arrays()
    health = np.array([[[0, 1, 1], [0, 0, 1], [1, 1, 1]]], dtype=np.uint8)
    a = replace(a, health=health, il_eligible=np.ones((3, 3, 2), dtype=np.uint8), add_limit=2)
    assert kernel(a, ((0,),), (1, 2)).ravel().tolist() == [1.0, 1.0, 1.0]
    shared = replace(a, health=np.array([[[0, 0, 1]] * 3], dtype=np.uint8))
    result = kernel(shared, ((0,), (1,)), (2,))
    assert result.sum() == 3.0 and (result[..., 2] > 0).sum() == 1
    unavailable = replace(a, health=np.zeros_like(a.health))
    assert not kernel(unavailable, ((0,),), (1, 2)).any()


def test_injury_return_ties_and_weekly_lock_availability(kernel):
    a = replace(small_arrays(), value=np.ones(3))
    assert kernel(a, ((0,),), (1, 2)).ravel().tolist() == [1.0, 2.0, 0.0]
    health = np.array([[[1, 0, 1], [1, 0, 1], [1, 1, 1]]], dtype=np.uint8)
    assert kernel(replace(a, health=health), ((1,),), (0, 2)).ravel().tolist() == [3, 0, 0]
    delayed = replace(a, weekly_lock=True, next_day=True)
    assert not kernel(delayed, ((0,),), (1, 2)).any()
    ineligible = replace(a, weekly_lock=True, il_eligible=np.zeros_like(a.il_eligible))
    assert not kernel(ineligible, ((0,),), (1, 2)).any()


@pytest.mark.parametrize("dimension", range(8))
def test_native_abi_rejects_invalid_dimensions(kernel, monkeypatch, dimension):
    function = kernel.function

    def invalid(*args):
        values = list(args)
        values[dimension] = -1 if dimension == 7 else 0
        return function(*values)

    monkeypatch.setattr(kernel, "function", invalid)
    with pytest.raises(SolverError, match="Invalid season dimensions"):
        kernel(small_arrays(), ((0,),), (1, 2))


def test_native_abi_accepts_zero_error_buffer_capacity(kernel, monkeypatch):
    function = kernel.function

    def invalid(*args):
        values = list(args)
        values[0] = 0
        values[-1] = 0
        assert function(*values) == -1
        assert values[-2].value == b""
        return -1

    monkeypatch.setattr(kernel, "function", invalid)
    with pytest.raises(SolverError, match="^management: $"):
        kernel(small_arrays(), ((0,),), (1, 2))


@pytest.mark.parametrize(
    "pointer,value,message",
    [
        (25, -1, "roster size"),
        (25, 2, "roster size"),
        (24, -1, "roster player"),
        (24, 3, "roster player"),
    ],
)
def test_native_abi_rejects_invalid_roster_buffers(kernel, monkeypatch, pointer, value, message):
    function = kernel.function
    corrupt = np.array([value], dtype=np.int32)

    def invalid(*args):
        values = list(args)
        values[pointer] = ctypes.c_void_p(corrupt.ctypes.data)
        return function(*values)

    monkeypatch.setattr(kernel, "function", invalid)
    with pytest.raises(SolverError, match=message):
        kernel(small_arrays(), ((0,),), (1, 2))


@pytest.mark.parametrize(
    "field,value",
    [
        ("il_eligible", np.zeros(3, dtype=np.uint8)),
        ("weeks", np.array([-1, 0, 0], dtype=np.int32)),
        ("week_count", 0),
        ("health", np.zeros((3, 3), dtype=np.uint8)),
        ("games", np.full((3, 3), 2, dtype=np.uint8)),
        ("priority", np.array([np.nan, 1.0, 1.0])),
    ],
)
def test_malformed_native_arrays_fail_before_abi(kernel, field, value):
    with pytest.raises(DataError, match="management"):
        kernel(replace(small_arrays(), **{field: value}), ((0,),), (1, 2))


def test_managed_contract_failures_and_local_memoization(kernel):
    manager, _, _ = reference_manager(kernel)
    players = tuple(
        AuctionPlayer(
            id=p.id,
            name=p.id,
            positions=tuple(
                pos
                for bit, pos in enumerate(manager.league.positions)
                if manager.masks[i] & (1 << bit)
            ),
            positions_confirmed=True,
            active=True,
            projected_price=1.0,
            fair=1.0,
            utility=float(manager.priority[i]),
        )
        for i, p in enumerate(manager.players)
    )
    source = ManagementInput(
        stat_ids=manager.stat_ids, sampling_ids=manager.ids, players=manager.players
    )
    variants = (
        source.model_copy(update={"players": (*source.players, source.players[0])}),
        source.model_copy(update={"sampling_ids": source.sampling_ids[:-1]}),
        source.model_copy(
            update={
                "players": tuple(p.model_copy(update={"game_days": ()}) for p in source.players)
            }
        ),
        source.model_copy(
            update={
                "players": (
                    source.players[0].model_copy(update={"expected_games": 1000.0}),
                    *source.players[1:],
                )
            }
        ),
        source.model_copy(
            update={
                "players": (source.players[0].model_copy(update={"means": ()}), *source.players[1:])
            }
        ),
        source.model_copy(
            update={
                "players": (
                    source.players[0].model_copy(
                        update={
                            "game_days": (
                                *source.players[0].game_days,
                                source.players[0].game_days[0],
                            )
                        }
                    ),
                    *source.players[1:],
                )
            }
        ),
    )
    for candidate in variants:
        with pytest.raises(DataError, match="management"):
            ManagedSeason(manager.league, manager.parameters, candidate, players, kernel)
    with pytest.raises(DataError, match="population"):
        ManagedSeason(manager.league, manager.parameters, source, players[:-1], kernel)
    empty_calendar = manager.league.model_copy(update={"matchups": ()})
    with pytest.raises(DataError, match="schedule"):
        ManagedSeason(empty_calendar, manager.parameters, source, players, kernel)
    wide = manager.league.model_copy(
        update={"positions": (*manager.league.positions, *(str(i) for i in range(65)))}
    )
    with pytest.raises(DataError, match="mask width"):
        ManagedSeason(wide, manager.parameters, source, players, kernel)
    assert manager.lineup(()) == manager.lineup(()) == ()
    assert manager.control(()) is manager.control(())
    with pytest.raises(DataError, match="duplicate"):
        manager.project((0, 0))
    weekly = manager.league.model_copy(
        update={
            "lineup": manager.league.lineup.model_copy(
                update={"lock_mode": "weekly", "lock_at": "first_game"}
            )
        }
    )
    first_game = ManagedSeason(weekly, manager.parameters, source, players, kernel)
    assert all(first_game.games[d].any() for d in np.flatnonzero(first_game.arrays().lock_days))
    sparse = source.model_copy(
        update={
            "players": tuple(
                p.model_copy(update={"game_days": (manager.days[0], manager.days[-1])})
                for p in source.players
            )
        }
    )
    no_games = ManagedSeason(weekly, manager.parameters, sparse, players, kernel)
    assert no_games.arrays().lock_days.sum() == 2


def test_weekly_period_lock_precedes_first_scheduled_game(kernel):
    original, _, _ = reference_manager(kernel)
    players = tuple(
        AuctionPlayer(
            id=p.id,
            name=p.id,
            positions=tuple(
                pos
                for bit, pos in enumerate(original.league.positions)
                if original.masks[i] & (1 << bit)
            ),
            positions_confirmed=True,
            active=True,
            projected_price=1.0,
            fair=1.0,
            utility=float(original.priority[i]),
        )
        for i, p in enumerate(original.players)
    )
    first_period = original.weeks[0]
    managed = tuple(
        p.model_copy(update={"game_days": tuple(d for d in p.game_days if d > first_period.start)})
        for p in original.players
    )
    source = ManagementInput(stat_ids=original.stat_ids, sampling_ids=original.ids, players=managed)
    daily = ManagedSeason(original.league, original.parameters, source, players, kernel)
    weekly = original.league.model_copy(
        update={
            "lineup": original.league.lineup.model_copy(
                update={"lock_mode": "weekly", "lock_at": "period_start"}
            )
        }
    )
    manager = ManagedSeason(weekly, original.parameters, source, players, kernel)
    lock_day = manager.weeks[0].start
    assert daily.days[0] > lock_day, "fixture must start NBA games after period lock"
    assert manager.days[0] == lock_day
    roster = tuple(range(manager.league.bench_slots + len(manager.league.starter_slots)))
    player = max(roster, key=lambda p: manager.priority[p])
    manager.health[:] = True
    for d, day in enumerate(manager.days):
        manager.health[:, d, player] = day != lock_day
    # Recovery after the period lock must not put this player into that week's lineup.
    result = kernel(manager.arrays(), (roster,), ())
    assert not result[:, 0, 0, player].any()
    first_game = weekly.model_copy(
        update={"lineup": weekly.lineup.model_copy(update={"lock_at": "first_game"})}
    )
    other = ManagedSeason(first_game, original.parameters, source, players, kernel)
    assert other.days == daily.days
