import ctypes
import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol, cast

import numpy as np
from numpy.typing import NDArray

from fba.contracts.auction import SolverError
from fba.contracts.base import DataError, Record, Text
from fba.contracts.data import Digest
from fba.contracts.season import SeasonArrays


class NativeCall(Protocol):
    def __call__(self, *args: object) -> int: ...


class NativeArtifact(Record):
    path: Text
    source_sha256: Digest
    binary_sha256: Digest


def compile_kernel() -> tuple[tempfile.TemporaryDirectory[str], Path]:
    compiler = shutil.which("c++")
    if compiler is None:
        raise SolverError("management: requires a local C++17 compiler")
    build = tempfile.TemporaryDirectory(prefix="fba-season-")
    path = Path(build.name) / "season.so"
    source = Path(__file__).parents[1] / "native/season.cpp"
    command = (
        compiler,
        "-std=c++17",
        "-O2",
        "-ffp-contract=off",
        "-fPIC",
        "-shared",
        str(source),
        "-o",
        str(path),
    )
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        build.cleanup()
        raise SolverError(f"management: native compilation failed: {exc}") from exc
    if result.returncode:
        build.cleanup()
        raise SolverError(f"management: native compilation failed: {result.stderr}")
    return build, path


class NativeKernel:
    def __init__(self, compiled: NativeArtifact | None = None) -> None:
        self.build: tempfile.TemporaryDirectory[str] | None = None
        source = Path(__file__).parents[1] / "native/season.cpp"
        if compiled is None:
            self.build, path = compile_kernel()
            compiled = NativeArtifact(
                path=str(path),
                source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                binary_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        self.artifact = compiled
        try:
            if (
                hashlib.sha256(source.read_bytes()).hexdigest() != compiled.source_sha256
                or hashlib.sha256(Path(compiled.path).read_bytes()).hexdigest()
                != compiled.binary_sha256
            ):
                raise SolverError("management: native source or compiled bytes changed")
            self.library = ctypes.CDLL(compiled.path)
        except (OSError, SolverError) as exc:
            self.close()
            raise SolverError(f"management: cannot load verified native kernel: {exc}") from exc
        function = self.library.fba_season
        function.argtypes = [ctypes.c_int] * 12 + [ctypes.c_void_p] * 16 + [ctypes.c_int]
        function.restype = ctypes.c_int
        self.function = cast(NativeCall, function)

    def close(self) -> None:
        if self.build is not None:
            self.build.cleanup()

    def __call__(
        self, arrays: SeasonArrays, rosters: tuple[tuple[int, ...], ...], pool: tuple[int, ...]
    ) -> NDArray[np.float64]:
        validate_arrays(arrays, rosters, pool)
        samples, days, n = arrays.health.shape
        roster = np.full((len(rosters), arrays.roster_capacity), -1, dtype=np.int32)
        for i, players in enumerate(rosters):
            roster[i, : len(players)] = players
        sizes = np.array([len(r) for r in rosters], dtype=np.int32)
        free = np.zeros(n, dtype=np.uint8)
        free[list(pool)] = 1
        counts = np.zeros((samples, len(rosters), arrays.week_count, n))
        error = ctypes.create_string_buffer(1024)
        buffers = (
            arrays.health,
            arrays.games,
            arrays.weeks,
            arrays.periods,
            arrays.masks,
            arrays.slots,
            arrays.priority,
            arrays.value,
            arrays.orders,
            arrays.il_eligible,
            arrays.lock_days,
            roster,
            sizes,
            free,
            counts,
        )
        status = self.function(
            n,
            days,
            arrays.week_count,
            samples,
            len(rosters),
            arrays.roster_capacity,
            len(arrays.slots),
            arrays.il_eligible.shape[1],
            arrays.add_limit,
            arrays.waiver_days,
            int(arrays.next_day),
            int(arrays.weekly_lock),
            *(ctypes.c_void_p(a.ctypes.data) for a in buffers),
            error,
            len(error),
        )
        if status:
            raise SolverError(f"management: {error.value.decode('utf-8', errors='replace')}")
        return counts


def validate_arrays(
    a: SeasonArrays, rosters: tuple[tuple[int, ...], ...], pool: tuple[int, ...]
) -> None:
    if a.health.ndim != 3:
        raise DataError("management.health: requires sample, day, player axes")
    if a.il_eligible.ndim != 2 or a.slots.ndim != 1:
        raise DataError("management: invalid eligibility or slot axes")
    samples, days, n = a.health.shape
    if (
        min(samples, days, n, a.week_count, a.roster_capacity, len(a.slots), len(rosters)) < 1
        or min(a.add_limit, a.waiver_days) < 0
    ):
        raise DataError("management: invalid dimensions or rule limits")
    specs = (
        (a.health, np.uint8, (samples, days, n)),
        (a.games, np.uint8, (days, n)),
        (a.weeks, np.int32, (days,)),
        (a.periods, np.int32, (days,)),
        (a.masks, np.uint64, (n,)),
        (a.slots, np.uint64, (len(a.slots),)),
        (a.priority, np.float64, (n,)),
        (a.value, np.float64, (n,)),
        (a.orders, np.int32, (samples, days, n)),
        (a.il_eligible, np.uint8, (n, a.il_eligible.shape[1])),
        (a.lock_days, np.uint8, (days,)),
    )
    for array, dtype, shape in specs:
        if (
            array.dtype != dtype
            or array.shape != shape
            or not array.flags.c_contiguous
            or not np.isfinite(array).all()
        ):
            raise DataError(
                "management: invalid native array dtype, shape, layout, or finite values"
            )
    if (
        np.any(a.health > 1)
        or np.any(a.games > 1)
        or np.any(a.il_eligible > 1)
        or np.any(a.lock_days > 1)
        or np.any(a.weeks < 0)
        or np.any(a.weeks >= a.week_count)
        or np.any(a.periods < 0)
    ):
        raise DataError("management: invalid health, eligibility or period values")
    if np.any(a.orders < 0) or np.any(a.orders >= n):
        raise DataError("management.orders: unknown player index")
    owned = tuple(p for r in rosters for p in r)
    if (
        len(set(owned)) != len(owned)
        or any(len(r) > a.roster_capacity for r in rosters)
        or any(p < 0 or p >= n for p in (*owned, *pool))
    ):
        raise DataError("management.rosters: invalid ownership, index, or capacity")
