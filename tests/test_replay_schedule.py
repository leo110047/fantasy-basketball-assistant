from dataclasses import replace
from datetime import UTC, datetime, time, timedelta

import numpy as np
import pytest
from test_backtest import replay_fixture
from test_managed import small_arrays

from fba.adapters.native import NativeKernel
from fba.auction.managed import ManagedSeason
from fba.auction.season import health_tape, replay
from fba.contracts.backtest import (
    ActualBox,
    ScheduledGame,
    ScheduledReplayInput,
    ScheduleObservation,
)
from fba.contracts.base import DataError
from fba.contracts.season import ManagementPolicy
from fba.core.replay_schedule import schedule_tape


@pytest.fixture(scope="module")
def kernel():
    native = NativeKernel()
    yield native
    native.close()


def scheduled_fixture(kernel, *, tactics=False):
    source, auction = replay_fixture(kernel)
    config = source.config.model_copy(
        update={
            "season": source.config.season.model_copy(
                update={
                    "starts_on": source.decision_times[0].date(),
                    "ends_on": source.decision_times[-1].date(),
                }
            ),
            "league": source.config.league.model_copy(update={"timezone": "UTC"}),
        }
    )
    source = source.model_copy(
        update={
            "config": config,
            "upgrades": tactics,
            "teams": tuple(
                t.model_copy(update={"streaming_slots": int(tactics)}) for t in source.teams
            ),
            "health": tuple(
                e.model_copy(update={"available": True, "status": "available"})
                for e in source.health
            ),
        }
    )
    current = ScheduledReplayInput(**{**source.model_dump(), "format_version": 2, "schedules": ()})
    return current, auction.model_copy(update={"config": config}), source


def observation(player, dates, published):
    return ScheduleObservation(
        player_id=player,
        published_at=published,
        games=tuple(
            ScheduledGame(day=d, tipoff=datetime.combine(d, time(12), UTC)) for d in sorted(dates)
        ),
        source_artifact="schedule.json",
    )


@pytest.mark.parametrize("lock", ["daily", "period_start", "first_game"])
def test_no_updates_preserves_fixed_calendar_decisions_and_scores(kernel, lock):
    current, auction, legacy = scheduled_fixture(kernel)
    if lock != "daily":
        league = current.config.league
        league = league.model_copy(
            update={
                "lineup": league.lineup.model_copy(update={"lock_mode": "weekly", "lock_at": lock})
            }
        )
        config = current.config.model_copy(update={"league": league})
        current, legacy, auction = (
            x.model_copy(update={"config": config}) for x in (current, legacy, auction)
        )
    old = replay(legacy, auction, "0" * 64, kernel)
    new = replay(current, auction, "0" * 64, kernel)
    assert new.events == old.events
    assert new.weekly_boxes == old.weekly_boxes
    assert new.adds == old.adds and new.outcomes == old.outcomes
    assert new.algorithm == "published-schedule-management-v1"
    assert old.algorithm == "causal-management-v2"


def test_game_moves_across_matchup_weeks_and_requires_its_new_box(kernel):
    source, auction, _ = scheduled_fixture(kernel)
    pid = source.teams[0].roster[0]
    auction = auction.model_copy(
        update={
            "players": tuple(
                p.model_copy(update={"utility": 10000.0}) if p.id == pid else p
                for p in auction.players
            )
        }
    )
    before = replay(source, auction, "0" * 64, kernel)
    player = next(p for p in auction.management.players if p.id == pid)
    old = next(d for d in player.game_days if d <= source.config.league.matchups[0].end)
    new = next(
        t.date()
        for t in source.decision_times
        if source.config.league.matchups[1].start
        <= t.date()
        <= source.config.league.matchups[1].end
        and t.date() not in player.game_days
    )
    event = observation(
        pid, (set(player.game_days) - {old}) | {new}, source.decision_times[0] - timedelta(hours=1)
    )
    changed = source.model_copy(
        update={
            "schedules": (event,),
            "actual": tuple(
                b.model_copy(update={"day": new}) if (b.player_id, b.day) == (pid, old) else b
                for b in source.actual
            ),
        }
    )
    after = replay(changed, auction, "1" * 64, kernel)
    p = after.player_ids.index(pid)
    d = after.days.index(new)
    assert any(e.kind == "lineup" and e.day == d and p in e.started for e in after.events)
    assert before.weekly_boxes != after.weekly_boxes
    with pytest.raises(DataError, match="outside player schedule"):
        replay(changed.model_copy(update={"actual": source.actual}), auction, "0" * 64, kernel)
    missing = tuple(b for b in changed.actual if (b.player_id, b.day) != (pid, new))
    with pytest.raises(DataError, match="requires every scheduled"):
        replay(changed.model_copy(update={"actual": missing}), auction, "0" * 64, kernel)


def cancellation_case(kernel):
    source, auction, _ = scheduled_fixture(kernel, tactics=True)
    cutoff = source.config.league.matchups[1].start
    pid = source.free_agents[0]
    players = tuple(
        p.model_copy(update={"game_days": tuple(d for d in p.game_days if d >= cutoff)})
        if p.id == pid
        else p
        for p in auction.management.players
    )
    auction = auction.model_copy(
        update={
            "management": auction.management.model_copy(update={"players": players}),
            "players": tuple(
                p.model_copy(update={"utility": 10000.0}) if p.id == pid else p
                for p in auction.players
            ),
        }
    )
    source = source.model_copy(
        update={"actual": tuple(b for b in source.actual if b.player_id != pid or b.day >= cutoff)}
    )
    event = observation(pid, (), datetime.combine(cutoff, time(1), UTC))
    changed = source.model_copy(
        update={
            "schedules": (event,),
            "actual": tuple(b for b in source.actual if b.player_id != pid),
        }
    )
    return source, changed, auction, cutoff, pid


def assert_public_prefix(source, changed, auction, cutoff, kernel):
    before, after = (replay(i, auction, "0" * 64, kernel) for i in (source, changed))
    end = before.days.index(cutoff)
    assert tuple(e for e in before.events if e.day < end) == tuple(
        e for e in after.events if e.day < end
    )
    assert tuple(t[0] for t in before.weekly_boxes) == tuple(t[0] for t in after.weekly_boxes)
    return before, after


def test_future_schedule_announcements_cannot_change_earlier_decisions(kernel, monkeypatch):
    source, changed, auction, cutoff, pid = cancellation_case(kernel)
    before, after = assert_public_prefix(source, changed, auction, cutoff, kernel)
    p = before.player_ids.index(pid)
    assert any(e.added == p and e.day < before.days.index(cutoff) for e in before.events)
    assert before.events != after.events
    original = ManagedSeason.schedule_on

    def leaking(self, day):
        return self.schedule.actual if self.schedule is not None else original(self, day)

    monkeypatch.setattr(ManagedSeason, "schedule_on", leaking)
    with pytest.raises(AssertionError):
        assert_public_prefix(source, changed, auction, cutoff, kernel)


def test_same_day_cancellation_after_selection_scores_zero_without_fake_dnp(kernel):
    source, auction, _ = scheduled_fixture(kernel)
    baseline = replay(source, auction, "0" * 64, kernel)
    selected = next(e for e in baseline.events if e.kind == "lineup" and e.started)
    pid, day = baseline.player_ids[selected.started[0]], baseline.days[selected.day]
    player = next(p for p in auction.management.players if p.id == pid)
    event = observation(
        pid, set(player.game_days) - {day}, source.decision_times[selected.day] + timedelta(hours=1)
    )
    changed = source.model_copy(
        update={
            "schedules": (event,),
            "actual": tuple(b for b in source.actual if (b.player_id, b.day) != (pid, day)),
        }
    )
    result = replay(changed, auction, "0" * 64, kernel)
    assert tuple(e for e in result.events if e.day <= selected.day) == tuple(
        e for e in baseline.events if e.day <= selected.day
    )
    assert result.weekly_boxes != baseline.weekly_boxes
    tape = schedule_tape(changed, auction.management)
    p = result.player_ids.index(pid)
    assert tape.known[selected.day][selected.day, p] and not tape.actual[selected.day, p]


@pytest.mark.parametrize(
    "failure",
    [
        "unknown",
        "duplicate_day",
        "outside",
        "retroactive",
        "duplicate_event",
        "future",
        "before_snapshot",
    ],
)
def test_invalid_schedule_changes_fail_explicitly(kernel, failure):
    source, auction, _ = scheduled_fixture(kernel)
    player = auction.management.players[0]
    event = observation(player.id, player.game_days, source.decision_times[0])
    if failure == "unknown":
        event = event.model_copy(update={"player_id": "unknown"})
    if failure == "duplicate_day":
        event = event.model_copy(update={"games": (*event.games, event.games[0])})
    if failure == "outside":
        event = observation(
            player.id, (source.config.season.ends_on + timedelta(days=1),), event.published_at
        )
    if failure == "retroactive":
        event = observation(player.id, (), source.decision_times[-1])
    if failure == "future":
        event = event.model_copy(update={"published_at": source.evaluated_at + timedelta(days=1)})
    if failure == "before_snapshot":
        event = event.model_copy(update={"published_at": source.config.season.snapshot_as_of})
    changes = (event, event) if failure == "duplicate_event" else (event,)
    with pytest.raises(DataError, match="replay.schedules"):
        schedule_tape(source.model_copy(update={"schedules": changes}), auction.management)


def test_unchanged_calendar_versions_share_storage_and_shuffle_is_stable(kernel):
    source, changed, auction, _, _ = cancellation_case(kernel)
    tape = schedule_tape(source, auction.management)
    assert all(x is tape.known[0] for x in tape.known)
    assert not np.shares_memory(tape.actual, tape.known[-1])
    player = auction.management.players[0]
    unchanged = observation(player.id, player.game_days, source.decision_times[0])
    changed = changed.model_copy(update={"schedules": (*changed.schedules, unchanged)})
    a = replay(changed, auction, "0" * 64, kernel)
    shuffled = changed.model_copy(
        update={
            "schedules": tuple(reversed(changed.schedules)),
            "actual": tuple(reversed(changed.actual)),
        }
    )
    assert a == replay(shuffled, auction, "0" * 64, kernel)


def test_weekly_acquisition_forecast_cannot_see_a_future_lock_delay(kernel):
    source, auction, _ = scheduled_fixture(kernel, tactics=True)
    league = source.config.league
    league = league.model_copy(
        update={
            "lineup": league.lineup.model_copy(
                update={"lock_mode": "weekly", "lock_at": "first_game"}
            )
        }
    )
    config = source.config.model_copy(update={"league": league})
    source = source.model_copy(update={"config": config})
    auction = auction.model_copy(update={"config": config})
    cutoff = league.matchups[1].start
    changed = source.model_copy(
        update={
            "schedules": tuple(
                observation(
                    p.id, set(p.game_days) - {cutoff}, datetime.combine(cutoff, time.min, UTC)
                )
                for p in auction.management.players
            )
        }
    )
    results = []
    for inputs in (source, changed):
        health, _ = health_tape(inputs, auction.management.sampling_ids)
        manager = ManagedSeason(
            league,
            config.model.fit.model_copy(update={"health_samples": 1}),
            auction.management,
            auction.players,
            kernel,
            health,
            schedule=schedule_tape(inputs, auction.management),
        )
        policy = ManagementPolicy(streaming_slots=(1, 1), reserve_adds=0, upgrades=True)
        results.append(manager.tactics(policy, config.model.management))
    end = tuple(t.date() for t in source.decision_times).index(cutoff)
    for field in ("acquired_short", "acquired_long"):
        np.testing.assert_array_equal(
            getattr(results[0], field)[:, :end], getattr(results[1], field)[:, :end]
        )


def test_weekly_native_locks_use_known_counts_and_score_late_added_games(kernel):
    # Player 0 was expected to play twice; player 1 three times. Only after the
    # lock is player 0's schedule expanded and player 1's reduced to one game.
    a = replace(
        small_arrays(),
        health=np.ones((1, 3, 3), dtype=np.uint8),
        games=np.array([[1, 1, 0], [1, 0, 0], [1, 0, 0]], dtype=np.uint8),
        priority=np.ones(3),
        il_eligible=np.zeros((3, 3, 0), dtype=np.uint8),
        roster_capacity=3,
        weekly_lock=True,
        known_week_games=np.array([[2, 3, 0], [2, 0, 0], [1, 0, 0]], dtype=np.int32),
    )
    assert kernel(a, ((0, 1, 2),), ()).ravel().tolist() == [0.0, 1.0, 0.0]
    assert kernel(replace(a, known_week_games=None), ((0, 1, 2),), ()).ravel().tolist() == [
        3.0,
        0.0,
        0.0,
    ]
    # A game added after the same-day lock also counts for an already locked
    # player, even though it was not in the calendar used to choose the lineup.
    counts = np.array([[2, 0, 0], [2, 0, 0], [1, 0, 0]], dtype=np.int32)
    locked = replace(a, known_week_games=counts)
    assert kernel(locked, ((0, 1, 2),), ()).ravel().tolist() == [3.0, 0.0, 0.0]


def test_weekly_replay_scores_game_added_after_its_lineup_locked(kernel):
    source, auction, _ = scheduled_fixture(kernel)
    league = source.config.league
    config = source.config.model_copy(
        update={
            "league": league.model_copy(
                update={
                    "lineup": league.lineup.model_copy(
                        update={"lock_mode": "weekly", "lock_at": "period_start"}
                    )
                }
            )
        }
    )
    day = source.decision_times[0].date()
    player = next(p for p in auction.management.players if p.id == source.teams[0].roster[0])
    player = player.model_copy(update={"game_days": tuple(d for d in player.game_days if d != day)})
    source = source.model_copy(
        update={
            "config": config,
            "actual": tuple(b for b in source.actual if (b.player_id, b.day) != (player.id, day)),
        }
    )
    auction = auction.model_copy(
        update={
            "config": config,
            "management": auction.management.model_copy(
                update={
                    "players": tuple(
                        player if p.id == player.id else p for p in auction.management.players
                    )
                }
            ),
            "players": tuple(
                p.model_copy(update={"utility": 10000.0}) if p.id == player.id else p
                for p in auction.players
            ),
        }
    )
    baseline = replay(source, auction, "0" * 64, kernel)
    changed = source.model_copy(
        update={
            "schedules": (
                observation(
                    player.id,
                    (*player.game_days, day),
                    source.decision_times[0] + timedelta(hours=1),
                ),
            ),
            "actual": (
                *source.actual,
                ActualBox(
                    player_id=player.id, day=day, stats=player.means, source_artifact="actual.json"
                ),
            ),
        }
    )
    result = replay(changed, auction, "0" * 64, kernel)
    np.testing.assert_allclose(
        np.array(result.weekly_boxes[0][0]) - baseline.weekly_boxes[0][0], player.means
    )
    assert result.weekly_boxes[1] == baseline.weekly_boxes[1]


@pytest.mark.parametrize(
    "bad",
    [
        np.ones((3, 3), dtype=float),
        np.ones((3, 2), dtype=np.int32),
        -np.ones((3, 3), dtype=np.int32),
        np.full((3, 3), 4, dtype=np.int32),
        np.ones((3, 6), dtype=np.int32)[:, ::2],
    ],
)
def test_native_rejects_invalid_known_counts_before_abi(kernel, monkeypatch, bad):
    def forbidden(*_args):
        pytest.fail("invalid schedule counts reached native ABI")

    monkeypatch.setattr(kernel, "function", forbidden)
    with pytest.raises(DataError, match="known_week_games"):
        kernel(replace(small_arrays(), known_week_games=bad), ((0,),), (1, 2))
