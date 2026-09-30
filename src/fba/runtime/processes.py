"""Bound the desktop's worker lifetime to its owning process."""

import ctypes
import os
import sys
from collections.abc import Generator
from contextlib import contextmanager
from multiprocessing import parent_process
from threading import Event, Thread

from fba.contracts.base import DataError


class BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64),
        ("job_time", ctypes.c_int64),
        ("flags", ctypes.c_ulong),
        ("minimum_working_set", ctypes.c_size_t),
        ("maximum_working_set", ctypes.c_size_t),
        ("active_process_limit", ctypes.c_ulong),
        ("affinity", ctypes.c_size_t),
        ("priority", ctypes.c_ulong),
        ("scheduling_class", ctypes.c_ulong),
    ]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", BasicLimits),
        ("io_counters", ctypes.c_uint64 * 6),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


@contextmanager
def owned_processes() -> Generator[None]:
    if sys.platform != "win32":
        yield
        return
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    create = library.CreateJobObjectW
    create.argtypes, create.restype = [ctypes.c_void_p, ctypes.c_wchar_p], ctypes.c_void_p
    configure = library.SetInformationJobObject
    configure.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
    configure.restype = ctypes.c_int
    assign = library.AssignProcessToJobObject
    assign.argtypes, assign.restype = [ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int
    current = library.GetCurrentProcess
    current.argtypes, current.restype = [], ctypes.c_void_p
    close = library.CloseHandle
    close.argtypes, close.restype = [ctypes.c_void_p], ctypes.c_int
    handle = create(None, None)
    if not handle:
        raise DataError(f"runtime.job: CreateJobObject failed ({ctypes.get_last_error()})")
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    try:
        if not configure(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise DataError(f"runtime.job: limit setup failed ({ctypes.get_last_error()})")
        if not assign(handle, current()):
            raise DataError(f"runtime.job: cannot own child lifetime ({ctypes.get_last_error()})")
        yield
    finally:
        # The app closes/drains its workers before normal teardown. Disarm the
        # parent-containing job before closing it; forced parent death leaves
        # KILL_ON_JOB_CLOSE armed and Windows terminates every descendant.
        limits.basic.flags = 0
        if not configure(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise DataError(f"runtime.job: cannot finish teardown ({ctypes.get_last_error()})")
        if not close(handle):
            raise DataError(f"runtime.job: cannot close handle ({ctypes.get_last_error()})")


def watch_parent() -> None:
    owner = parent_process()
    if owner is None:
        raise DataError("runtime.worker: parent watcher requires a spawned worker")

    def monitor() -> None:
        interval = Event()
        while not interval.wait(0.2):
            if not owner.is_alive():
                os._exit(1)

    Thread(target=monitor, name="fba-parent-monitor", daemon=True).start()
