from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context

from fba.adapters.native import NativeArtifact, NativeKernel
from fba.contracts.auction import MarginalRequest, Plan
from fba.contracts.season import MarginalFeature
from fba.core.auction import run_caps
from fba.core.fit import marginal_batch
from fba.core.managed import ManagedSeason
from fba.core.portfolio import Portfolio


def managed_batch(
    request: MarginalRequest, compiled: NativeArtifact
) -> tuple[MarginalFeature, ...]:
    kernel = NativeKernel(compiled)
    try:
        manager = ManagedSeason(
            request.league, request.parameters, request.management, request.players, kernel
        )
        manager.set_pool(request.pool)
        return marginal_batch(manager, request.tasks)
    finally:
        kernel.close()


class AuctionSession:
    """Own worker lifetimes outside the pure calculation core; never cache results."""

    def __init__(self, workers: int) -> None:
        if workers < 1:
            raise ValueError("workers: must be positive")
        self.workers = workers
        self.pool = ProcessPoolExecutor(max_workers=workers, mp_context=get_context("spawn"))
        self.kernel: NativeKernel | None = None

    def close(self) -> None:
        self.pool.shutdown(wait=True, cancel_futures=True)
        if self.kernel is not None:
            self.kernel.close()

    def native(self) -> NativeKernel:
        if self.kernel is None:
            self.kernel = NativeKernel()
        return self.kernel

    def features(self, request: MarginalRequest) -> tuple[MarginalFeature, ...]:
        kernel = self.native()
        requests = tuple(
            request.model_copy(update={"tasks": request.tasks[i :: self.workers]})
            for i in range(self.workers)
        )
        futures = tuple(
            self.pool.submit(managed_batch, request, kernel.artifact) for request in requests
        )
        return tuple(
            sorted(
                (feature for future in futures for feature in future.result()),
                key=lambda f: f.index,
            )
        )

    def caps(
        self, portfolio: Portfolio, base: Plan, candidates: tuple[int, ...]
    ) -> tuple[tuple[tuple[int, float | None, bool], ...], int]:
        width = max(1, (len(candidates) + self.workers * 2 - 1) // (self.workers * 2))
        chunks = tuple(candidates[i : i + width] for i in range(0, len(candidates), width))
        futures = tuple(self.pool.submit(run_caps, portfolio, base, chunk) for chunk in chunks)
        results: dict[int, tuple[int, float | None, bool]] = {}
        calls = 0
        for chunk, future in zip(chunks, futures, strict=True):
            values, count = future.result()
            calls += count
            results.update(zip(chunk, values, strict=True))
        return tuple(results[i] for i in candidates), calls
