from argparse import ArgumentTypeError
from collections.abc import Callable
from concurrent.futures import CancelledError, Future, ProcessPoolExecutor, wait
from math import ceil
from multiprocessing import get_context
from multiprocessing.connection import Connection
from os import process_cpu_count
from threading import Event
from types import TracebackType
from typing import cast


def worker_limit(value: str) -> int:
    if value == "auto":
        # Assumption: reserve half the available logical CPUs for the desktop/coordinators.
        return max(1, (process_cpu_count() or 1) // 2)
    try:
        limit = int(value)
    except ValueError as exc:
        raise ArgumentTypeError("workers: use auto or a positive integer") from exc
    if limit < 1:
        raise ArgumentTypeError("workers: use auto or a positive integer")
    return limit


def parallelism(limit: int, items: int, work: int, grain: int) -> int:
    """Limit active batches, not idle resident processes; no auction result is cached."""
    return min(limit, items, max(1, ceil(work / grain)))


def check_cancelled(signal: Connection | None) -> None:
    # The byte is deliberately not consumed: every worker observes the same cancellation.
    if signal is not None and signal.poll():
        raise CancelledError("calculation superseded")


def check_current(cancelled: Event) -> None:
    if cancelled.is_set():
        raise CancelledError("calculation superseded")


class WorkBatch:
    """One call's child tasks and cooperative cancellation pipe, closed after children finish."""

    def __init__(self, cancelled: Event | None) -> None:
        self.cancelled = cancelled
        self.receiver, self.sender = get_context("spawn").Pipe(duplex=False)
        self.futures: list[Future[object]] = []
        self.signalled = False

    def __enter__(self) -> "WorkBatch":
        return self

    def stop(self) -> None:
        if not self.signalled:
            self.sender.send_bytes(b"stop")
            self.signalled = True
        for future in self.futures:
            future.cancel()

    def check(self) -> None:
        if self.cancelled is not None and self.cancelled.is_set():
            self.stop()
            raise CancelledError("calculation superseded")

    def submit[T](
        self, pool: ProcessPoolExecutor, function: Callable[..., T], *args: object
    ) -> Future[T]:
        self.check()
        future = pool.submit(function, *args, self.receiver)
        self.futures.append(cast(Future[object], future))
        return future

    def result[T](self, future: Future[T]) -> T:
        while True:
            self.check()
            # wait distinguishes a polling timeout from a TimeoutError raised by the task.
            if wait((future,), timeout=0.05).done:
                self.check()
                return future.result()

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        try:
            if kind is not None:
                self.stop()
            wait(self.futures)
        finally:
            self.receiver.close()
            self.sender.close()
