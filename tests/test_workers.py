import os
from argparse import ArgumentTypeError
from concurrent.futures import CancelledError, ProcessPoolExecutor
from multiprocessing import get_context
from threading import Event

import pytest

from fba.apps.workers import WorkBatch, check_cancelled, parallelism, worker_limit


def waiting_worker(started, signal):
    started.send(os.getpid())
    assert signal.poll(10), "worker never received cancellation"
    check_cancelled(signal)


def echo_worker(value, signal):
    check_cancelled(signal)
    return value, os.getpid()


def failing_worker(signal):
    check_cancelled(signal)
    raise TimeoutError("solver deadline")


@pytest.mark.parametrize("cpus,expected", [(12, 9), (8, 6), (3, 2), (2, 1), (1, 1), (None, 1)])
def test_auto_ceiling_reserves_desktop_capacity(monkeypatch, cpus, expected):
    monkeypatch.setattr("fba.apps.workers.process_cpu_count", lambda: cpus)
    assert worker_limit("auto") == expected
    assert worker_limit("4") == 4


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "many"])
def test_bad_worker_ceiling_is_rejected(value):
    with pytest.raises(ArgumentTypeError, match="positive integer"):
        worker_limit(value)


def test_parallel_batches_scale_with_work_and_respect_both_limits():
    assert parallelism(6, 0, 0, 32) == 0
    assert parallelism(6, 5, 5, 32) == 1
    assert parallelism(6, 80, 80, 32) == 3
    assert parallelism(6, 264, 264, 32) == 6
    assert parallelism(6, 2, 1000, 32) == 2
    assert parallelism(1, 264, 264, 32) == 1


def test_actual_spawned_work_is_cancelled_and_shared_pool_stays_usable():
    context = get_context("spawn")
    first_read, first_write = context.Pipe(duplex=False)
    second_read, second_write = context.Pipe(duplex=False)
    first_cancel, second_cancel = Event(), Event()
    try:
        with ProcessPoolExecutor(max_workers=2, mp_context=context) as pool:
            with WorkBatch(first_cancel) as first, WorkBatch(second_cancel) as second:
                old = first.submit(pool, waiting_worker, first_write)
                other = second.submit(pool, waiting_worker, second_write)
                assert first_read.poll(5) and second_read.poll(5), [
                    future.exception() if future.done() else "running" for future in (old, other)
                ]
                pids = {first_read.recv(), second_read.recv()}
                assert len(pids) == 2
                first_cancel.set()
                with pytest.raises(CancelledError):
                    first.result(old)
                # A fresh request can use the released worker while the other job remains alive.
                with WorkBatch(None) as fresh:
                    value, pid = fresh.result(fresh.submit(pool, echo_worker, "latest"))
                    assert value == "latest" and pid in pids
                assert not other.done(), "cancelling one request interrupted another"
                second_cancel.set()
                with pytest.raises(CancelledError):
                    second.result(other)
            assert old.done() and other.done()
            assert old.exception().__class__ is CancelledError
            assert other.exception().__class__ is CancelledError
    finally:
        for connection in (first_read, first_write, second_read, second_write):
            connection.close()


def test_cancel_before_submission_starts_no_child_work():
    event = Event()
    event.set()
    with ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn")) as pool:
        with WorkBatch(event) as batch:
            with pytest.raises(CancelledError):
                batch.submit(pool, echo_worker, "stale")
            assert not batch.futures


def test_cli_defaults_to_auto_and_explicit_value_is_a_ceiling(monkeypatch):
    from fba.apps.cli import parser

    monkeypatch.setattr("fba.apps.workers.process_cpu_count", lambda: 12)
    command = ["serve", "input.json", "--draft", "draft.json", "--log", "log.jsonl"]
    assert parser().parse_args(command).workers == 9
    assert parser().parse_args([*command, "--workers", "3"]).workers == 3


def test_worker_failure_is_reported_and_sibling_work_is_drained():
    context = get_context("spawn")
    reader, writer = context.Pipe(duplex=False)
    try:
        with ProcessPoolExecutor(max_workers=2, mp_context=context) as pool:
            with pytest.raises(TimeoutError, match="solver deadline"):
                with WorkBatch(None) as batch:
                    sibling = batch.submit(pool, waiting_worker, writer)
                    assert reader.poll(5)
                    reader.recv()
                    batch.result(batch.submit(pool, failing_worker))
            assert sibling.done() and isinstance(sibling.exception(), CancelledError)
    finally:
        reader.close()
        writer.close()


def test_parent_kernel_cancellation_keeps_native_results_and_stops_next_call():
    import numpy as np
    from test_managed import small_arrays

    from fba.adapters.native import NativeKernel
    from fba.apps.auction import CancellableKernel

    native = NativeKernel()
    try:
        event = Event()
        wrapped = CancellableKernel(native, event)
        arrays = small_arrays()
        np.testing.assert_array_equal(
            wrapped(arrays, ((0,),), (1, 2)), native(arrays, ((0,),), (1, 2))
        )
        event.set()
        with pytest.raises(CancelledError):
            wrapped(arrays, ((0,),), (1, 2))
    finally:
        native.close()
