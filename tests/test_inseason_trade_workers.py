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
