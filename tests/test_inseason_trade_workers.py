"""Real spawned trade workers preserve the serial oracle and request lifetime."""

import os
from concurrent.futures import CancelledError
from concurrent.futures.process import BrokenProcessPool
from multiprocessing import get_context
from multiprocessing.connection import Connection
from threading import Event, Timer

import pytest
from inseason_support import simulation
from test_inseason_trade_policy import preferences

from fba.apps.inseason import trade_workers
from fba.apps.inseason.trade_workers import TradeWorkers, trade_batch, trade_work
from fba.apps.workers import WorkBatch
from fba.contracts.base import DataError
from fba.contracts.inseason import CalculationTimeout
from fba.contracts.inseason_results import TradeSummary
from fba.inseason.matchup import Simulation
from fba.inseason.trades import (
    TradeCandidate,
    eligible_trade_bundles,
    evaluate_trade,
    search_trades,
)


def ranked_case(mode):
    sim = simulation(mode=mode, teams=4)
    sim.players = sim.players.model_copy(
        update={
            "players": tuple(p.model_copy(update={"public_rank": 100}) for p in sim.players.players)
        }
    )
    return sim


def worker_exit():
    os._exit(1)


def started_search(
    sim: Simulation, candidates: tuple[TradeCandidate, ...], ready: Connection, signal: Connection
):
    ready.send_bytes(b"started")
    return trade_batch(sim, candidates, 1, signal)


@pytest.mark.parametrize("mode", ("h2h_one_win", "h2h_each_category"))
@pytest.mark.parametrize("size", (1, 2))
def test_spawned_complete_search_matches_serial_payload_and_top_ten(monkeypatch, mode, size):
    # Only the allocation threshold is forced; real processes run the actual core.
    monkeypatch.setattr(trade_workers, "trade_work", lambda *_: 8_000_001)
    serial, parallel = ranked_case(mode), ranked_case(mode)
    prefs = preferences()
    opponent = "team1" if size == 2 else None
    expected = search_trades(serial, prefs, opponent, size, lambda _: None)
    workers = TradeWorkers(2)
    progress = []
    try:
        actual = workers.search(parallel, prefs, opponent, size, progress.append, Event())
        assert parallel.trade_search_counts["workers"] == 2
        assert parallel.trade_search_counts["eligible"] == (
            parallel.trade_search_counts["full_effects"] + parallel.trade_search_counts["bounded"]
        )
        assert progress[-1] == 1 and progress == sorted(progress)
        reference = expected if size == 1 else expected[:10]
        result = actual if size == 1 else actual[:10]
        assert [r.model_dump(mode="json") for r in result] == [
            {k: r.model_dump(mode="json")[k] for k in TradeSummary.model_fields} for r in reference
        ]
        for row in reference[:3]:
            detail = evaluate_trade(
                parallel, parallel.snapshot.mine, row.opponent, row.send, row.receive
            )
            assert detail.model_dump(mode="json") == row.model_dump(mode="json")
    finally:
        workers.close()


def test_small_or_empty_work_stays_local_and_closed_owner_rejects_new_search():
    sim, prefs = ranked_case("h2h_one_win"), preferences()
    candidates = eligible_trade_bundles(sim, prefs, None, 1)
    assert trade_work(sim, candidates) < 4_000_000
    workers = TradeWorkers(9)
    try:
        result = workers.search(sim, prefs, None, 1, lambda _: None, Event())
        assert result and workers.pool is None and sim.trade_search_counts["workers"] == 1
    finally:
        workers.close()
    with pytest.raises(DataError, match="closed"):
        workers.search(sim, prefs, None, 1, lambda _: None, Event())


def test_cancel_during_spawn_drains_workers_without_partial_result(monkeypatch):
    monkeypatch.setattr(trade_workers, "trade_work", lambda *_: 8_000_001)
    workers, cancelled = TradeWorkers(2), Event()
    timer = Timer(0.03, cancelled.set)
    timer.start()
    try:
        with pytest.raises((CancelledError, CalculationTimeout)):
            workers.search(
                ranked_case("h2h_one_win"), preferences(), None, 1, lambda _: None, cancelled
            )
    finally:
        timer.join()
        workers.close()
    assert workers.pool is None and workers.closed


def test_cancel_after_worker_started_interrupts_actual_trade_search_and_drains_pool():
    sim = ranked_case("h2h_one_win")
    candidates = eligible_trade_bundles(sim, preferences(), None, 1) * 1_000
    ready, notify = get_context("spawn").Pipe(duplex=False)
    workers, cancelled = TradeWorkers(2), Event()
    try:
        with pytest.raises(CancelledError), WorkBatch(cancelled) as batch:
            future = batch.submit(workers.working_pool(), started_search, sim, candidates, notify)
            assert ready.poll(20), "worker did not start the actual search"
            assert ready.recv_bytes() == b"started"
            cancelled.set()
            batch.result(future)
        assert isinstance(future.exception(), CalculationTimeout)
    finally:
        workers.close()
        ready.close()
        notify.close()


def test_broken_worker_is_explicit_and_next_request_recreates_pool(monkeypatch):
    monkeypatch.setattr(trade_workers, "trade_work", lambda *_: 8_000_001)
    workers = TradeWorkers(2)
    pool = workers.working_pool()
    try:
        with pytest.raises(BrokenProcessPool):
            pool.submit(worker_exit).result(timeout=20)
        with pytest.raises(DataError, match="worker exited"):
            workers.search(
                ranked_case("h2h_one_win"), preferences(), None, 1, lambda _: None, Event()
            )
        assert workers.pool is None
        result = workers.search(
            ranked_case("h2h_one_win"), preferences(), None, 1, lambda _: None, Event()
        )
        assert result and workers.pool is not pool
    finally:
        workers.close()


@pytest.mark.parametrize("parallel", (False, True))
def test_cancellation_retains_complete_candidates_and_next_search_works(monkeypatch, parallel):
    if parallel:
        monkeypatch.setattr(trade_workers, "trade_work", lambda *_: 8_000_001)
    workers, cancelled = TradeWorkers(2), Event()
    sim = ranked_case("h2h_one_win")
    updates = []

    def progress(value):
        updates.append(workers.progress)
        if value > 0:
            cancelled.set()

    try:
        with pytest.raises((CancelledError, CalculationTimeout)):
            workers.search(sim, preferences(), None, 1, progress, cancelled)
        saved = tuple(workers.completed_trades)
        assert saved
        assert workers.progress.completed == len(saved)
        assert workers.progress.total == sim.trade_search_counts["eligible"]
        assert all(row.total == workers.progress.total for row in updates)
        assert [row.completed for row in updates] == sorted(row.completed for row in updates)
        assert len({(r.opponent, r.send, r.receive) for r in saved}) == len(saved)
        cancelled.clear()
        complete = workers.search(
            ranked_case("h2h_one_win"), preferences(), None, 1, lambda _: None, cancelled
        )
        reference = {(r.opponent, r.send, r.receive): r for r in complete}
        assert all(row == reference[row.opponent, row.send, row.receive] for row in saved)
        assert workers.progress.completed == workers.progress.total
    finally:
        workers.close()


def test_cancel_inside_first_candidate_does_not_count_or_publish_it(monkeypatch):
    workers, cancelled = TradeWorkers(1), Event()
    sim = ranked_case("h2h_one_win")

    def interrupted(*_, **__):
        cancelled.set()
        sim.check_limits()

    monkeypatch.setattr("fba.inseason.trades.trade_effects", interrupted)
    try:
        with pytest.raises(CalculationTimeout):
            workers.search(sim, preferences(), None, 1, lambda _: None, cancelled)
        assert workers.progress.total > 0
        assert workers.progress.completed == sim.trade_search_counts["full_effects"] == 0
        assert workers.completed_trades == []
    finally:
        workers.close()


@pytest.mark.parametrize("size", (1, 2))
@pytest.mark.parametrize("parallel", (False, True))
def test_interactive_search_does_not_enforce_the_performance_target(monkeypatch, size, parallel):
    if parallel:
        monkeypatch.setattr(trade_workers, "trade_work", lambda *_: 8_000_001)
    sim = ranked_case("h2h_one_win")
    reference = search_trades(
        ranked_case("h2h_one_win"),
        preferences(),
        "team1" if size == 2 else None,
        size,
        lambda _: None,
    )
    sim.params = sim.params.model_copy(
        update={
            "budgets": {
                name: target.model_copy(update={"value": 1e-12})
                for name, target in sim.params.budgets.items()
            }
        }
    )
    workers = TradeWorkers(2 if parallel else 1)
    try:
        result = workers.search(
            sim, preferences(), "team1" if size == 2 else None, size, lambda _: None, Event()
        )
        assert [r.model_dump(mode="json") for r in result] == [
            {k: r.model_dump(mode="json")[k] for k in TradeSummary.model_fields} for r in reference
        ]
        assert workers.progress.total > 0
        assert sim.deadline is None
        assert sim.enforce_time_targets
        assert workers.progress.completed == workers.progress.total
    finally:
        workers.close()


def test_interactive_policy_preserves_explicit_caller_deadline_and_restores_scope():
    sim = ranked_case("h2h_one_win")
    sim.deadline = (0, "explicit caller limit")
    workers = TradeWorkers(1)
    try:
        with pytest.raises(CalculationTimeout, match="explicit caller limit"):
            workers.search(sim, preferences(), None, 1, lambda _: None, Event())
        assert sim.enforce_time_targets
        assert sim.deadline == (0, "explicit caller limit")
    finally:
        workers.close()


def test_interactive_policy_reaches_roster_and_injury_scenarios():
    from fba.inseason.injury_returns import return_scenario, reuse_return_plans
    from fba.inseason.recommendations import changed_simulation

    sim = ranked_case("h2h_one_win")
    mine = sim.snapshot.mine
    with sim.without_time_targets():
        clones = (
            changed_simulation(sim, mine, ()),
            return_scenario(sim, mine, ()),
            reuse_return_plans(sim, (mine,), {mine: ()})[0],
        )
        for child in clones:
            assert not child.enforce_time_targets
            assert not child.season().enforce_time_targets
            with child.budget("week"):
                assert child.deadline is None
            child.deadline = (0, "explicit scenario limit")
            with pytest.raises(CalculationTimeout, match="explicit scenario limit"):
                child.check_limits()
    assert sim.enforce_time_targets
