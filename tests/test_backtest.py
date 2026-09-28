from datetime import UTC, datetime, time, timedelta

import numpy as np
import pytest
from test_managed import reference_manager
from test_season import management_parameters

from fba.adapters.native import NativeKernel
from fba.contracts.auction import AuctionInput, AuctionPlayer
from fba.contracts.backtest import ActualBox, HealthObservation, Pairing, ReplayInput, ReplayTeam
from fba.contracts.base import DataError
from fba.contracts.config import SeasonModel
from fba.contracts.season import ManagementInput
from fba.core.managed import ManagedSeason
from fba.core.season import health_tape, replay


@pytest.fixture(scope="module")
def kernel():
    native = NativeKernel()
    yield native
    native.close()


def replay_fixture(kernel):
    manager, _, _ = reference_manager(kernel, 1)
    league = manager.league.model_copy(
        update={
            "teams": 2,
            "matchups": tuple(
                w.model_copy(
                    update={"phase": "playoff" if i == len(manager.weeks) - 1 else "regular"}
                )
                for i, w in enumerate(manager.weeks)
            ),
            "playoffs": manager.league.playoffs.model_copy(
                update={"team_count": 2, "week_ids": (manager.weeks[-1].id,), "byes": 0}
            ),
        }
    )
    from test_auction import config

    c = config()
    model = SeasonModel.model_validate(
        {
            **c.model.model_dump(),
            "format_version": 6,
            "management": management_parameters(manager, 1).model_dump(),
        }
    )
    c = c.model_copy(update={"league": league, "model": model})
    players = tuple(
        AuctionPlayer(
            id=p.id,
            name=p.id,
            positions=tuple(
                pos for bit, pos in enumerate(league.positions) if manager.masks[i] & (1 << bit)
            ),
            positions_confirmed=True,
            active=True,
            projected_price=1.0,
            fair=1.0,
            utility=float(manager.priority[i]),
        )
        for i, p in enumerate(manager.players)
    )
    auction = AuctionInput(
        format_version=1,
        config=c,
        artifacts=(),
        snapshot_sha256="1" * 64,
        calculation_sha256="2" * 64,
        players=players,
        management=ManagementInput(
            stat_ids=manager.stat_ids, sampling_ids=manager.ids, players=manager.players
        ),
    )
    times = tuple(datetime.combine(d, time.min, UTC) for d in manager.days)
    health = tuple(
        HealthObservation(
            player_id=p,
            published_at=t,
            available=bool(manager.health[0, d, i]),
            status="available"
            if manager.health[0, d, i]
            else manager.parameters.unavailable_status,
            source_artifact="health.json",
        )
        for d, t in enumerate(times)
        for i, p in enumerate(manager.ids)
    )
    actual = tuple(
        ActualBox(
            player_id=p.id,
            day=day,
            stats=tuple(float(v) for v in p.means),
            source_artifact="actual.json",
        )
        for p in manager.players
        for day in p.game_days
    )
    teams = tuple(
        ReplayTeam(
            id=f"team-{t}",
            seed=t + 1,
            roster=manager.ids[t * 10 : (t + 1) * 10],
            streaming_slots=t + 1,
        )
        for t in range(2)
    )
    source = ReplayInput(
        format_version=1,
        config=c,
        artifacts=(),
        auction_path="auction/auction-input.json",
        auction_sha256="3" * 64,
        evaluated_at=datetime(2027, 7, 1, tzinfo=UTC),
        information_mode="published",
        decision_times=times,
        teams=teams,
        free_agents=manager.ids[20:],
        upgrades=True,
        health=health,
        actual=actual,
        pairings=tuple(
            Pairing(week_id=w.id, home=teams[0].id, away=teams[1].id)
            for w in league.matchups
            if w.phase == "regular"
        ),
    )
    return source, auction


def test_replay_future_health_and_actual_cannot_change_completed_weeks(kernel):
    source, auction = replay_fixture(kernel)
    baseline = replay(source, auction, "0" * 64, kernel)
    cutoff = source.config.league.matchups[0].end + timedelta(days=1)
    later = source.model_copy(
        update={
            "health": tuple(
                e.model_copy(
                    update={
                        "available": not e.available,
                        "status": "available"
                        if not e.available
                        else source.config.model.fit.unavailable_status,
                    }
                )
                if e.published_at.date() >= cutoff
                else e
                for e in source.health
            ),
            "actual": tuple(
                b.model_copy(update={"stats": tuple(v * 10 for v in b.stats)})
                if b.day >= cutoff
                else b
                for b in source.actual
            ),
        }
    )
    changed = replay(later, auction, "1" * 64, kernel)
    assert tuple(t[0] for t in baseline.weekly_boxes) == tuple(t[0] for t in changed.weekly_boxes)
    assert baseline.outcomes[0] == changed.outcomes[0]
    end = next(i for i, d in enumerate(baseline.days) if d == cutoff)
    assert tuple(e for e in baseline.events if e.day < end) == tuple(
        e for e in changed.events if e.day < end
    )
    assert any(e.kind == "stream" for e in baseline.events)
    assert baseline.weekly_boxes != changed.weekly_boxes


def test_causal_check_detects_future_health_forecast_bug(kernel, monkeypatch):
    source, auction = replay_fixture(kernel)
    original = ManagedSeason.tactics

    def leaking(self, policy, parameters):
        from dataclasses import replace

        t = original(self, policy, parameters)
        leaked = np.array(
            [
                [
                    (self.health[s, d:] * self.games[d:]).sum(axis=0) * self.value
                    for d in range(self.d)
                ]
                for s in range(self.parameters.health_samples)
            ],
            dtype=float,
        )
        return replace(t, short_values=leaked, long_values=leaked)

    monkeypatch.setattr(ManagedSeason, "tactics", leaking)
    a = replay(source, auction, "0" * 64, kernel)
    cutoff = source.config.league.matchups[0].end + timedelta(days=1)
    later = source.model_copy(
        update={
            "health": tuple(
                e.model_copy(update={"available": not e.available})
                if e.published_at.date() >= cutoff
                else e
                for e in source.health
            )
        }
    )
    b = replay(later, auction, "1" * 64, kernel)
    assert tuple(t[0] for t in a.weekly_boxes) != tuple(t[0] for t in b.weekly_boxes)


def test_input_shuffle_preserves_decisions_and_scoring(kernel):
    source, auction = replay_fixture(kernel)
    a = replay(source, auction, "0" * 64, kernel)
    shuffled = source.model_copy(
        update={
            "health": tuple(reversed(source.health)),
            "actual": tuple(reversed(source.actual)),
            "free_agents": tuple(reversed(source.free_agents)),
        }
    )
    b = replay(shuffled, auction, "0" * 64, kernel)
    assert a == b


def test_health_tape_rejects_missing_duplicate_and_unknown_initial_observations(kernel):
    source, auction = replay_fixture(kernel)
    ids = auction.management.sampling_ids
    for health in (
        source.health[1:],
        (*source.health, source.health[0]),
        (*source.health, source.health[0].model_copy(update={"player_id": "absent"})),
    ):
        with pytest.raises(DataError, match="health"):
            health_tape(source.model_copy(update={"health": health}), ids)


def expanded_fixture(kernel, teams, weekly, stream_slots):
    from fba.contracts.config import Category, Linear, StarterSlot, Term

    source, auction = replay_fixture(kernel)
    league = source.config.league
    slots = (
        *league.starter_slots,
        StarterSlot(id="utility", label="UTIL", eligible_positions=league.positions),
    )
    categories = tuple(c for c in league.categories if c.id not in ("OREB", "DD", "A/T")) + (
        Category(
            id="TO",
            label="TO",
            formula=Linear(kind="linear", terms=(Term(stat_id="TO", coefficient=1.0),)),
            direction="lower",
            comparison_decimals=0,
            tie_value=0.5,
        ),
    )
    league = league.model_copy(
        update={
            "teams": teams,
            "starter_slots": slots,
            "bench_slots": 3,
            "categories": categories,
            "transactions": league.transactions.model_copy(update={"adds_per_period": 3}),
            "lineup": league.lineup.model_copy(
                update={"lock_mode": "weekly" if weekly else "daily", "lock_at": "period_start"}
            ),
            "matchups": tuple(
                w.model_copy(update={"phase": "regular" if i == 0 else "playoff"})
                for i, w in enumerate(league.matchups)
            ),
            "playoffs": league.playoffs.model_copy(
                update={
                    "team_count": 6,
                    "week_ids": tuple(w.id for w in league.matchups[1:]),
                    "byes": 2,
                }
            ),
        }
    )
    count = teams * (len(slots) + 3) + 30
    original = auction.management
    managed = tuple(
        original.players[i % len(original.players)].model_copy(update={"id": f"p{i:04}"})
        for i in range(count)
    )
    players = tuple(
        auction.players[i % len(auction.players)].model_copy(
            update={"id": p.id, "positions": league.positions}
        )
        for i, p in enumerate(managed)
    )
    health_by_id = {
        p.id: tuple(e for e in source.health if e.player_id == p.id) for p in original.players
    }
    health = tuple(
        e.model_copy(update={"player_id": p.id})
        for i, p in enumerate(managed)
        for e in health_by_id[original.players[i % len(original.players)].id]
    )
    actual = tuple(
        ActualBox(player_id=p.id, day=day, stats=p.means, source_artifact="actual.json")
        for p in managed
        for day in p.game_days
    )
    size = len(slots) + 3
    rosters = tuple(
        ReplayTeam(
            id=f"t{i:02}",
            seed=i + 1,
            roster=tuple(p.id for p in managed[i * size : (i + 1) * size]),
            streaming_slots=stream_slots,
        )
        for i in range(teams)
    )
    config = source.config.model_copy(update={"league": league})
    auction = auction.model_copy(
        update={
            "config": config,
            "players": players,
            "management": original.model_copy(
                update={"players": managed, "sampling_ids": tuple(p.id for p in managed)}
            ),
        }
    )
    source = source.model_copy(
        update={
            "config": config,
            "teams": rosters,
            "health": health,
            "actual": actual,
            "free_agents": tuple(p.id for p in managed[teams * size :]),
            "pairings": tuple(
                Pairing(week_id=league.matchups[0].id, home=rosters[i].id, away=rosters[i + 1].id)
                for i in range(0, teams, 2)
            ),
        }
    )
    return source, auction


def assert_management_invariants(source, result):
    by_id = {pid: i for i, pid in enumerate(result.player_ids)}
    known, _ = health_tape(source, result.player_ids)
    injured_count = sum(g.count for g in source.config.league.injury_slots)
    current = {}
    used = {}
    injury = {}
    for event_index, event in enumerate(result.events):
        period = next(
            p.id
            for p in source.config.league.transactions.add_periods
            if p.start <= result.days[event.day] <= p.end
        )
        key = (event.team, period)
        if event.kind in ("injury_add", "upgrade", "stream"):
            used[key] = used.get(key, 0) + 1
            injury[key] = injury.get(key, 0) + int(event.kind == "injury_add")
            assert known[0, event.day, event.added]
            assert used[key] <= source.config.league.transactions.adds_per_period
            if event.kind != "injury_add":
                assert used[key] <= source.config.league.transactions.adds_per_period - max(
                    0, source.config.model.management.reserve_adds - injury[key]
                )
        if event.kind == "lineup":
            assert len(event.active) <= len(source.teams[event.team].roster)
            assert len(event.injured) <= injured_count
            assert set(event.started) <= set(event.active)
            assert len(event.started) <= len(source.config.league.starter_slots)
            current[event.team] = (*event.active, *event.injured)
            if len(current) == len(source.teams):
                held = tuple(p for roster in current.values() for p in roster)
                # Check global ownership after every team has completed this day.
                if (
                    event_index + 1 == len(result.events)
                    or result.events[event_index + 1].day != event.day
                ):
                    assert len(set(held)) == len(held)
    assert set(by_id) == set(result.player_ids)


@pytest.mark.parametrize(
    "teams,weekly,stream_slots",
    [(12, False, k) for k in range(4)] + [(16, True, k) for k in range(4)],
)
def test_configured_complete_replay_reserve_and_ownership(kernel, teams, weekly, stream_slots):
    source, auction = expanded_fixture(kernel, teams, weekly, stream_slots)
    result = replay(source, auction, "0" * 64, kernel)
    assert len(source.config.league.categories) == 9
    assert_management_invariants(source, result)
    assert result.champion in {t.id for t in source.teams}
    assert (
        len([o for o in result.outcomes if o.week_id in source.config.league.playoffs.week_ids])
        == 7
    )


def test_replay_rejects_invalid_runtime_state_and_incomplete_actuals(kernel):
    from fba.contracts.config import AuctionModel

    source, auction = replay_fixture(kernel)
    a, b = source.teams
    candidates = (
        source.model_copy(update={"teams": (a,)}),
        source.model_copy(update={"teams": (a, a)}),
        source.model_copy(update={"teams": (a, b.model_copy(update={"seed": a.seed}))}),
        source.model_copy(update={"teams": (a, b.model_copy(update={"roster": a.roster}))}),
        source.model_copy(update={"free_agents": ("unknown",)}),
        source.model_copy(update={"teams": (a.model_copy(update={"roster": a.roster[:-1]}), b)}),
        source.model_copy(
            update={"teams": (a.model_copy(update={"streaming_slots": len(a.roster) + 1}), b)}
        ),
        source.model_copy(
            update={"pairings": (source.pairings[0].model_copy(update={"week_id": "unknown"}),)}
        ),
        source.model_copy(
            update={"pairings": (source.pairings[0].model_copy(update={"home": "unknown"}),)}
        ),
        source.model_copy(
            update={"pairings": (source.pairings[0].model_copy(update={"away": "unknown"}),)}
        ),
        source.model_copy(update={"pairings": (*source.pairings, source.pairings[0])}),
        source.model_copy(update={"pairings": source.pairings[1:]}),
        source.model_copy(update={"actual": (*source.actual, source.actual[0])}),
        source.model_copy(
            update={
                "actual": (
                    source.actual[0].model_copy(update={"player_id": "unknown"}),
                    *source.actual[1:],
                )
            }
        ),
        source.model_copy(
            update={
                "actual": (source.actual[0].model_copy(update={"stats": ()}), *source.actual[1:])
            }
        ),
        source.model_copy(
            update={
                "actual": (
                    source.actual[0].model_copy(
                        update={"day": source.actual[0].day - timedelta(days=100)}
                    ),
                    *source.actual[1:],
                )
            }
        ),
        source.model_copy(update={"actual": source.actual[1:]}),
        source.model_copy(update={"decision_times": source.decision_times[:-1]}),
    )
    for candidate in candidates:
        with pytest.raises(DataError, match="replay|management.health"):
            replay(candidate, auction, "0" * 64, kernel)
    with pytest.raises(DataError, match="config"):
        replay(source, auction.model_copy(update={"management": None}), "0" * 64, kernel)
    data = source.config.model.model_dump()
    del data["management"]
    data["format_version"] = 5
    config = source.config.model_copy(update={"model": AuctionModel.model_validate(data)})
    with pytest.raises(DataError, match="format_version 6"):
        replay(
            source.model_copy(update={"config": config}),
            auction.model_copy(update={"config": config}),
            "0" * 64,
            kernel,
        )
    week = source.config.league.matchups[-1].model_copy(
        update={
            "id": "extra",
            "phase": "postseason",
            "start": source.config.league.matchups[-1].end + timedelta(days=1),
            "end": source.config.league.matchups[-1].end + timedelta(days=7),
        }
    )
    config = source.config.model_copy(
        update={
            "league": source.config.league.model_copy(
                update={"matchups": (*source.config.league.matchups, week)}
            )
        }
    )
    with pytest.raises(DataError, match="cover all configured"):
        replay(
            source.model_copy(
                update={
                    "config": config,
                    "pairings": (*source.pairings, Pairing(week_id="extra", home=a.id, away=b.id)),
                }
            ),
            auction.model_copy(update={"config": config}),
            "0" * 64,
            kernel,
        )


def test_future_unpublished_observations_are_ignored_and_empty_health_fails(kernel):
    source, auction = replay_fixture(kernel)
    ids = auction.management.sampling_ids
    a = health_tape(source, ids)
    future = source.health[-1].model_copy(
        update={"published_at": source.evaluated_at, "available": not source.health[-1].available}
    )
    b = health_tape(source.model_copy(update={"health": (*source.health, future)}), ids)
    np.testing.assert_array_equal(a[0], b[0])
    with pytest.raises(DataError, match="missing initial"):
        health_tape(source.model_copy(update={"health": ()}), ids)


def test_weekly_calendar_requires_one_authoritative_first_period(kernel):
    from fba.core.managed import management_calendar

    source, auction = replay_fixture(kernel)
    league = source.config.league.model_copy(
        update={
            "lineup": source.config.league.lineup.model_copy(
                update={"lock_mode": "weekly", "lock_at": "period_start"}
            ),
            "matchups": (),
        }
    )
    days = tuple(d for p in auction.management.players for d in p.game_days)
    with pytest.raises(DataError, match="one matchup period"):
        management_calendar(league, days)
