"""Distribute complete trade candidates using the desktop's existing worker policy."""

from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from multiprocessing import get_context
from multiprocessing.connection import Connection
from threading import Event, Lock

from fba.apps.workers import POLL_INTERVAL, WorkBatch, cancellation_check, parallelism, worker_limit
from fba.contracts.base import DataError
from fba.contracts.inseason import InseasonPreferences
from fba.contracts.inseason_results import TradeSummary
from fba.inseason.matchup import Simulation
from fba.inseason.season import playoff_probability, season_forecasts, season_value
from fba.inseason.trades import (
    TradeCandidate,
    eligible_trade_bundles,
    evaluate_trade_bundles,
    rank_trades,
)
from fba.runtime.processes import watch_parent


def trade_batch(
    sim: Simulation, candidates: tuple[TradeCandidate, ...], size: int, signal: Connection
) -> tuple[tuple[TradeSummary, ...], dict[str, int]]:
    sim.cancelled = cancellation_check(signal)
    result = evaluate_trade_bundles(sim, candidates, size, lambda _: None, details=False)
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
        sim.cancelled = cancelled.is_set
        with sim.budget("trade_one" if size == 1 else "trade_many"):
            candidates = eligible_trade_bundles(sim, preferences, opponent, size)
            # Same four-million sample/team/day setup grain as auction features.
            workers = parallelism(
                self.limit, len(candidates), trade_work(sim, candidates), 4_000_000
            )
            sim.trade_search_counts["workers"] = workers
            if workers <= 1:
                return evaluate_trade_bundles(sim, candidates, size, progress, details=False)
            baseline = sim.season()
            baseline.trade_search_counts = sim.trade_search_counts
            teams = {sim.snapshot.mine, *(row[0] for row in candidates)}
            for team in sorted(teams):
                if size == 1:
                    season_value(baseline, team)
                else:
                    season_forecasts(baseline, team)
            playoff_probability(baseline)
            sim.check_limits()
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
        results: list[TradeSummary] = []
        completed = 0
        pool = self.working_pool()
        try:
            with WorkBatch(cancelled) as batch:
                futures = {
                    batch.submit(pool, trade_batch, source, chunk, size): len(chunk)
                    for chunk in chunks
                }
                pending = set(futures)
                while pending:
                    batch.check()
                    sim.check_limits()
                    done, pending = wait(
                        pending, timeout=POLL_INTERVAL, return_when=FIRST_COMPLETED
                    )
                    for future in done:
                        rows, counts = batch.result(future)
                        results.extend(rows)
                        for key in ("full_effects", "bounded"):
                            sim.trade_search_counts[key] += counts[key]
                        completed += futures[future]
                        progress(completed / len(candidates))
        except BrokenProcessPool as exc:
            with self.lock:
                if self.pool is pool:
                    self.pool = None
            pool.shutdown(wait=True, cancel_futures=True)
            raise DataError("trade search: calculation worker exited; retry the search") from exc
        return rank_trades(sim, results)
