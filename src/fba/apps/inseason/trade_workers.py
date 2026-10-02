"""Distribute complete trade candidates using the desktop's existing worker policy."""

from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from functools import partial
from multiprocessing import get_context
from multiprocessing.connection import Connection
from threading import Event, Lock
from typing import NamedTuple, cast

from fba.apps.workers import POLL_INTERVAL, WorkBatch, cancellation_check, parallelism, worker_limit
from fba.contracts.base import DataError
from fba.contracts.inseason import InseasonPreferences
from fba.contracts.inseason_results import TradeSearchProgress, TradeSummary
from fba.inseason.matchup import Simulation
from fba.inseason.season import playoff_probability, season_forecasts, season_value
from fba.inseason.trades import (
    TradeCandidate,
    eligible_trade_bundles,
    evaluate_trade_bundles,
    rank_trades,
)
from fba.runtime.processes import watch_parent


class CandidateUpdate(NamedTuple):
    trade: TradeSummary | None
    full_effects: int
    bounded: int


def trade_batch(
    sim: Simulation,
    candidates: tuple[TradeCandidate, ...],
    size: int,
    signal: Connection,
    updates: Connection | None = None,
) -> tuple[tuple[TradeSummary, ...], dict[str, int]]:
    sim.cancelled = cancellation_check(signal)

    def finished(trade: TradeSummary | None) -> None:
        if updates is not None:
            updates.send(
                CandidateUpdate(
                    trade,
                    sim.trade_search_counts["full_effects"],
                    sim.trade_search_counts["bounded"],
                )
            )

    result = evaluate_trade_bundles(
        sim, candidates, size, lambda _: None, details=False, on_candidate=finished
    )
    sim.check_limits()
    return result, sim.trade_search_counts


def trade_work(sim: Simulation, candidates: tuple[TradeCandidate, ...]) -> int:
    today = sim.as_of.astimezone(sim.zone).date()
    days = sum(
        (w.end - max(w.start, today)).days + 1
        for w in sim.league.matchups
        if w.phase == "regular" and w.end >= today
    )
    free = sum(f.status == "free" for f in sim.snapshot.free_agents)
    capacity = len(sim.league.starter_slots) + sim.league.bench_slots
    # Estimate sample/team/days, including the complete unequal-roster choices.
    comparisons = sum(
        2 + abs(len(send) - len(receive)) * (free + capacity) for _, send, receive in candidates
    )
    return days * sim.params.season_simulations.value * comparisons


def batch_source(sim: Simulation) -> Simulation:
    source = Simulation(
        sim.league,
        sim.params,
        sim.players,
        sim.priors,
        sim.ledger,
        sim.snapshot,
        sim.as_of,
        sim.samples,
        untouchable=tuple(sim.untouchable),
    )
    source.transitions = sim.transitions.copy()
    source.acceptance_fit = sim.acceptance_fit
    source.deadline = sim.deadline
    source.enforce_time_targets = sim.enforce_time_targets
    source.project_injury_returns = sim.project_injury_returns
    for name in (
        "projections",
        "projection_profiles",
        "priority_cache",
        "injury_plan_cache",
        "joint_weeks",
        "count_weeks",
        "team_cache",
        "expected_cache",
        "matchup_cache",
        "forecast_cache",
        "standings_point_cache",
        "season_score_cache",
    ):
        setattr(source, name, getattr(sim, name).copy())
    source.playoff_probabilities = (
        sim.playoff_probabilities.copy() if sim.playoff_probabilities is not None else None
    )
    return source


class TradeWorkers:
    """Jobs owns a lazy pool; small searches keep their existing in-process caches."""

    def __init__(self, limit: int | None = None) -> None:
        self.limit = worker_limit("auto") if limit is None else worker_limit(str(limit))
        self.pool: ProcessPoolExecutor | None = None
        self.lock = Lock()
        self.closed = False
        self.progress = TradeSearchProgress()
        self.completed_trades: list[TradeSummary] = []

    def reset_progress(self) -> None:
        self.progress = TradeSearchProgress()
        self.completed_trades = []

    def completed_candidate(self, trade: TradeSummary | None) -> None:
        if trade is not None:
            self.completed_trades.append(trade)
        self.progress = self.progress.model_copy(update={"completed": self.progress.completed + 1})

    def close(self) -> None:
        with self.lock:
            self.closed = True
            pool, self.pool = self.pool, None
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)

    def working_pool(self) -> ProcessPoolExecutor:
        with self.lock:
            if self.closed:
                raise DataError("trade search: calculation workers are closed")
            if self.pool is None:
                self.pool = ProcessPoolExecutor(
                    max_workers=self.limit,
                    mp_context=get_context("spawn"),
                    initializer=watch_parent,
                )
            return self.pool

    def search(
        self,
        sim: Simulation,
        preferences: InseasonPreferences,
        opponent: str | None,
        size: int,
        progress: Callable[[float], None],
        cancelled: Event,
    ) -> tuple[TradeSummary, ...]:
        with self.lock:
            if self.closed:
                raise DataError("trade search: calculation workers are closed")
        self.reset_progress()
        sim.trade_search_counts = {}
        sim.cancelled = cancelled.is_set
        with sim.without_time_targets():
            # Interactive searches end on completion or explicit user cancellation.
            candidates = eligible_trade_bundles(sim, preferences, opponent, size)
            self.progress = TradeSearchProgress(phase="evaluating", total=len(candidates))
            # Same four-million sample/team/day setup grain as auction features.
            workers = parallelism(
                self.limit, len(candidates), trade_work(sim, candidates), 4_000_000
            )
            sim.trade_search_counts["workers"] = workers
            if workers <= 1:
                return evaluate_trade_bundles(
                    sim,
                    candidates,
                    size,
                    progress,
                    details=False,
                    on_candidate=self.completed_candidate,
                )
            baseline = sim.season()
            baseline.trade_search_counts = sim.trade_search_counts
            teams = {sim.snapshot.mine, *(row[0] for row in candidates)}
            self.progress = self.progress.model_copy(
                update={"phase": "baseline", "baseline_total": len(teams)}
            )
            for team in sorted(teams):
                if size == 1:
                    season_value(baseline, team)
                else:
                    season_forecasts(baseline, team)
                self.progress = self.progress.model_copy(
                    update={"baseline_completed": self.progress.baseline_completed + 1}
                )
            self.progress = self.progress.model_copy(update={"phase": "playoffs"})
            playoff_probability(baseline)
            sim.check_limits()
            self.progress = self.progress.model_copy(update={"phase": "evaluating"})
            return self.distribute(baseline, candidates, size, workers, progress, cancelled)

    def distribute(
        self,
        sim: Simulation,
        candidates: tuple[TradeCandidate, ...],
        size: int,
        workers: int,
        progress: Callable[[float], None],
        cancelled: Event,
    ) -> tuple[TradeSummary, ...]:
        source = batch_source(sim)
        # Two contiguous batches per worker let idle cores take remaining work
        # while keeping nearby candidates' opponent/draw reuse within a batch.
        batches = min(len(candidates), workers * 2) if size == 1 else workers
        width = (len(candidates) + batches - 1) // batches
        chunks = (
            tuple(candidates[i : i + width] for i in range(0, len(candidates), width))
            if size == 1
            else tuple(candidates[i::workers] for i in range(workers))
        )
        pool = self.working_pool()
        channels = cast(
            list[tuple[Connection, Connection]],
            [get_context("spawn").Pipe(duplex=False) for _ in chunks],
        )
        received = {reader: (0, 0) for reader, _ in channels}
        try:
            with WorkBatch(cancelled) as batch:
                error: Exception | None = None
                try:
                    for chunk, (_, sender) in zip(chunks, channels, strict=True):
                        batch.submit(
                            pool, partial(trade_batch, updates=sender), source, chunk, size
                        )
                except Exception as exc:
                    error = exc
                    batch.stop()
                pending = set(batch.futures)
                # Keep draining progress while cancelled tasks unwind: a worker
                # may be publishing its last complete candidate into a full pipe.
                while pending:
                    self.receive_updates(sim, received, progress)
                    if error is None:
                        try:
                            batch.check()
                            sim.check_limits()
                        except Exception as exc:
                            error = exc
                            batch.stop()
                    done, pending = wait(
                        pending, timeout=POLL_INTERVAL, return_when=FIRST_COMPLETED
                    )
                    for future in done:
                        try:
                            future.result()
                        except Exception as exc:
                            if error is None:
                                error = exc
                                batch.stop()
                self.receive_updates(sim, received, progress)
                if error is not None:
                    raise error
        except BrokenProcessPool as exc:
            with self.lock:
                if self.pool is pool:
                    self.pool = None
            pool.shutdown(wait=True, cancel_futures=True)
            raise DataError("trade search: calculation worker exited; retry the search") from exc
        finally:
            for reader, sender in channels:
                reader.close()
                sender.close()
        return rank_trades(sim, self.completed_trades)

    def receive_updates(
        self,
        sim: Simulation,
        received: dict[Connection, tuple[int, int]],
        progress: Callable[[float], None],
    ) -> None:
        for reader in received:
            while reader.poll():
                update = cast(CandidateUpdate, reader.recv())
                previous = received[reader]
                sim.trade_search_counts["full_effects"] += update.full_effects - previous[0]
                sim.trade_search_counts["bounded"] += update.bounded - previous[1]
                received[reader] = update.full_effects, update.bounded
                self.completed_candidate(update.trade)
                assert self.progress.total is not None and self.progress.total > 0
                progress(self.progress.completed / self.progress.total)
