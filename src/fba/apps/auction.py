from collections.abc import Generator
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from multiprocessing import get_context
from multiprocessing.connection import Connection
from threading import Event, Lock

from fba.adapters.native import NativeArtifact, NativeKernel
from fba.apps.workers import WorkBatch, check_cancelled, check_current, parallelism
from fba.contracts.auction import MarginalRequest, Plan, SolverError
from fba.contracts.season import MarginalFeature, SeasonArrays, SeasonRun, TacticalArrays
from fba.core.auction import run_caps
from fba.core.fit import marginal_batch
from fba.core.managed import FloatArray, ManagedSeason, management_calendar
from fba.core.portfolio import Portfolio


def managed_batch(
    request: MarginalRequest, compiled: NativeArtifact, signal: Connection | None = None
) -> tuple[MarginalFeature, ...]:
    check_cancelled(signal)
    kernel = NativeKernel(compiled)
    try:
        manager = ManagedSeason(
            request.league,
            request.parameters,
            request.management,
            request.players,
            kernel,
            pricing=request.pricing,
            tactics=request.tactics,
        )
        manager.set_pool(request.pool)
        values: list[MarginalFeature] = []
        for task in request.tasks:
            check_cancelled(signal)
            values.extend(marginal_batch(manager, (task,)))
        check_cancelled(signal)
        return tuple(values)
    finally:
        kernel.close()


def cap_batch(
    portfolio: Portfolio, base: Plan, candidates: tuple[int, ...], signal: Connection
) -> tuple[tuple[tuple[int, float | None, bool], ...], int]:
    values: list[tuple[int, float | None, bool]] = []
    calls = 0
    for candidate in candidates:
        check_cancelled(signal)
        value, count = run_caps(portfolio, base, (candidate,))
        values.extend(value)
        calls += count
    check_cancelled(signal)
    return tuple(values), calls


class CancellableKernel:
    """Check request currency between complete native calls; the shared kernel stays stateless."""

    def __init__(self, kernel: NativeKernel, cancelled: Event) -> None:
        self.kernel, self.cancelled = kernel, cancelled

    def __call__(
        self, arrays: SeasonArrays, rosters: tuple[tuple[int, ...], ...], pool: tuple[int, ...]
    ) -> FloatArray:
        return self.run(arrays, rosters, pool, None, False).counts

    def run(
        self,
        arrays: SeasonArrays,
        rosters: tuple[tuple[int, ...], ...],
        pool: tuple[int, ...],
        tactics: TacticalArrays | None,
        trace: bool,
        *,
        primary_only: bool = False,
    ) -> SeasonRun:
        check_current(self.cancelled)
        result = self.kernel.run(arrays, rosters, pool, tactics, trace, primary_only=primary_only)
        check_current(self.cancelled)
        return result


class AuctionSession:
    """Own worker lifetimes outside the pure calculation core; never cache results."""

    def __init__(self, workers: int) -> None:
        if workers < 1:
            raise ValueError("workers: must be positive")
        self.workers = workers
        self.pool = ProcessPoolExecutor(max_workers=workers, mp_context=get_context("spawn"))
        self.pool_lock = Lock()
        self.closed = False
        self.kernel: NativeKernel | None = None

    def close(self) -> None:
        with self.pool_lock:
            self.closed = True
            pool = self.pool
        pool.shutdown(wait=True, cancel_futures=True)
        if self.kernel is not None:
            self.kernel.close()

    @contextmanager
    def working_pool(self) -> Generator[ProcessPoolExecutor]:
        with self.pool_lock:
            if self.closed:
                raise SolverError("auction session is closed")
            pool = self.pool
        try:
            yield pool
        except BrokenProcessPool as exc:
            retired = False
            with self.pool_lock:
                # Both modes can observe the same dead pool; replace that generation only once.
                if not self.closed and self.pool is pool:
                    self.pool = ProcessPoolExecutor(
                        max_workers=self.workers, mp_context=get_context("spawn")
                    )
                    retired = True
            if retired:
                pool.shutdown(wait=True, cancel_futures=True)
            raise SolverError(
                "calculation worker exited; retry the saved draft calculation"
            ) from exc

    def native(self) -> NativeKernel:
        if self.kernel is None:
            self.kernel = NativeKernel()
        return self.kernel

    def features(
        self, request: MarginalRequest, *, cancelled: Event | None = None
    ) -> tuple[MarginalFeature, ...]:
        if not request.tasks:
            return ()
        days = len(
            management_calendar(
                request.league, tuple(d for p in request.management.players for d in p.game_days)
            )
        )
        work = (
            sum(1 + len(t.opponents) for t in request.tasks)
            * days
            * request.parameters.health_samples
        )
        # Assumption: roughly four million sample/team/days amortize one worker's setup.
        workers = parallelism(self.workers, len(request.tasks), work, 4_000_000)
        kernel = self.native()
        requests = tuple(
            request.model_copy(update={"tasks": request.tasks[i::workers]}) for i in range(workers)
        )
        with self.working_pool() as pool, WorkBatch(cancelled) as batch:
            futures = tuple(
                batch.submit(pool, managed_batch, request, kernel.artifact) for request in requests
            )
            return tuple(
                sorted(
                    (feature for future in futures for feature in batch.result(future)),
                    key=lambda f: f.index,
                )
            )

    def caps(
        self,
        portfolio: Portfolio,
        base: Plan,
        candidates: tuple[int, ...],
        *,
        cancelled: Event | None = None,
    ) -> tuple[tuple[tuple[int, float | None, bool], ...], int]:
        if not candidates:
            return (), 0
        # Assumption: avoid starting parallel batches for fewer than 32 candidate caps.
        workers = parallelism(self.workers, len(candidates), len(candidates), 32)
        width = (len(candidates) + workers - 1) // workers
        chunks = tuple(candidates[i : i + width] for i in range(0, len(candidates), width))
        results: dict[int, tuple[int, float | None, bool]] = {}
        calls = 0
        with self.working_pool() as pool, WorkBatch(cancelled) as batch:
            futures = tuple(
                batch.submit(pool, cap_batch, portfolio, base, chunk) for chunk in chunks
            )
            for chunk, future in zip(chunks, futures, strict=True):
                values, count = batch.result(future)
                calls += count
                results.update(zip(chunk, values, strict=True))
        return tuple(results[i] for i in candidates), calls
