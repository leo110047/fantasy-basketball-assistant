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
from fba.contracts.season import SeasonArrays, SeasonEvent, SeasonRun, TacticalArrays


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


class NativeOptions(ctypes.Structure):
    _fields_ = [
        ("reserve", ctypes.c_int),
        ("candidates", ctypes.c_int),
        ("upgrades", ctypes.c_int),
        ("capacity", ctypes.c_int),
        ("minimum_gain", ctypes.c_double),
        ("opportunity_cost", ctypes.c_double),
        ("flex", ctypes.c_void_p),
        ("short_order", ctypes.c_void_p),
        ("long_order", ctypes.c_void_p),
        ("short_values", ctypes.c_void_p),
        ("long_values", ctypes.c_void_p),
        ("acquired_short", ctypes.c_void_p),
        ("acquired_long", ctypes.c_void_p),
        ("adds", ctypes.c_void_p),
        ("events", ctypes.c_void_p),
        ("emitted", ctypes.c_void_p),
    ]


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
        function.argtypes = [ctypes.c_int] * 13 + [ctypes.c_void_p] * 17 + [ctypes.c_int]
        function.restype = ctypes.c_int
        self.function = cast(NativeCall, function)

    def close(self) -> None:
        if self.build is not None:
            self.build.cleanup()

    def __call__(
        self, arrays: SeasonArrays, rosters: tuple[tuple[int, ...], ...], pool: tuple[int, ...]
    ) -> NDArray[np.float64]:
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
        if primary_only and trace:
            raise DataError("management: primary-only scoring cannot produce a full trace")
        validate_arrays(arrays, rosters, pool)
        validate_tactics(arrays, rosters, tactics)
        samples, days, n = arrays.health.shape
        roster = np.full((len(rosters), arrays.roster_capacity), -1, dtype=np.int32)
        for i, players in enumerate(rosters):
            roster[i, : len(players)] = players
        sizes = np.array([len(r) for r in rosters], dtype=np.int32)
        free = np.zeros(n, dtype=np.uint8)
        free[list(pool)] = 1
        scored_teams = 1 if primary_only else len(rosters)
        counts = np.zeros((samples, scored_teams, arrays.week_count, n))
        adds = np.zeros((samples, len(rosters), arrays.week_count, 3), dtype=np.int32)
        capacity = (
            days
            * len(rosters)
            * (2 * arrays.il_eligible.shape[-1] + 2 * arrays.roster_capacity + 2)
            if trace
            else 0
        )
        width = 8 + arrays.roster_capacity + arrays.il_eligible.shape[-1] + len(arrays.slots)
        if capacity * width > np.iinfo(np.int32).max:
            raise DataError("management.trace: native integer capacity exceeded")
        events = np.full((capacity, width), -1, dtype=np.int32)
        emitted = np.zeros(1, dtype=np.int32)
        flex = np.array(tactics.policy.streaming_slots if tactics else (), dtype=np.int32)
        options = NativeOptions(
            tactics.policy.reserve_adds if tactics else 0,
            tactics.candidate_limit if tactics else 0,
            int(tactics.policy.upgrades) if tactics else 0,
            capacity,
            tactics.minimum_gain if tactics else 0.0,
            tactics.opportunity_cost if tactics else 0.0,
            flex.ctypes.data if tactics else None,
            tactics.short_orders.ctypes.data if tactics else None,
            tactics.long_orders.ctypes.data if tactics else None,
            tactics.short_values.ctypes.data if tactics else None,
            tactics.long_values.ctypes.data if tactics else None,
            tactics.acquired_short.ctypes.data if tactics else None,
            tactics.acquired_long.ctypes.data if tactics else None,
            adds.ctypes.data,
            events.ctypes.data if trace else None,
            emitted.ctypes.data,
        )
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
            arrays.il_eligible.shape[-1],
            arrays.add_limit,
            arrays.waiver_days,
            int(arrays.next_day),
            int(arrays.weekly_lock),
            scored_teams,
            *(ctypes.c_void_p(a.ctypes.data) for a in buffers),
            ctypes.byref(options),
            error,
            len(error),
        )
        if status:
            raise SolverError(f"management: {error.value.decode('utf-8', errors='replace')}")
        return SeasonRun(counts, adds, decode_events(events[: int(emitted[0])], arrays))


def decode_events(events: NDArray[np.int32], arrays: SeasonArrays) -> tuple[SeasonEvent, ...]:
    kinds = (
        "return_release",
        "return_drop",
        "activate",
        "il",
        "injury_add",
        "upgrade",
        "stream",
        "lineup",
    )
    result: list[SeasonEvent] = []
    for e in events:
        active, injured, started = ((), (), ())
        if kinds[int(e[2])] == "lineup":
            active = tuple(int(p) for p in e[8 : 8 + int(e[5])])
            start = 8 + arrays.roster_capacity
            injured = tuple(int(p) for p in e[start : start + int(e[6])])
            start += arrays.il_eligible.shape[-1]
            started = tuple(int(p) for p in e[start : start + int(e[7])])
        result.append(
            SeasonEvent(
                day=int(e[0]),
                team=int(e[1]),
                kind=kinds[int(e[2])],
                dropped=int(e[3]) if e[3] >= 0 else None,
                added=int(e[4]) if e[4] >= 0 else None,
                active=active,
                injured=injured,
                started=started,
            )
        )
    return tuple(result)


def validate_tactics(
    a: SeasonArrays, rosters: tuple[tuple[int, ...], ...], t: TacticalArrays | None
) -> None:
    if t is None:
        return
    if (
        len(t.policy.streaming_slots) != len(rosters)
        or any(k > len(r) for k, r in zip(t.policy.streaming_slots, rosters, strict=True))
        or not 0 <= t.policy.reserve_adds <= a.add_limit
        or not 1 <= t.candidate_limit <= np.iinfo(np.int32).max
        or t.minimum_gain < 0
        or t.opportunity_cost < 0
        or not np.isfinite([t.minimum_gain, t.opportunity_cost]).all()
    ):
        raise DataError(
            "management.policy: invalid streaming slots, reserve, or candidate parameters"
        )
    for array, dtype in (
        (t.short_values, np.float64),
        (t.long_values, np.float64),
        (t.acquired_short, np.float64),
        (t.acquired_long, np.float64),
        (t.short_orders, np.int32),
        (t.long_orders, np.int32),
    ):
        if (
            array.shape != a.health.shape
            or array.dtype != dtype
            or not array.flags.c_contiguous
            or not np.isfinite(array).all()
        ):
            raise DataError("management.policy: invalid forecast arrays")
    if any(
        np.any(order < 0) or np.any(order >= a.health.shape[-1])
        for order in (t.short_orders, t.long_orders)
    ):
        raise DataError("management.policy: invalid candidate ordering")


def validate_arrays(
    a: SeasonArrays, rosters: tuple[tuple[int, ...], ...], pool: tuple[int, ...]
) -> None:
    if a.health.ndim != 3:
        raise DataError("management.health: requires sample, day, player axes")
    if a.il_eligible.ndim != 3 or a.slots.ndim != 1:
        raise DataError("management: invalid eligibility or slot axes")
    samples, days, n = a.health.shape
    validate_native_integers(a, len(rosters))
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
        (a.il_eligible, np.uint8, (days, n, a.il_eligible.shape[-1])),
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


def validate_native_integers(a: SeasonArrays, teams: int) -> None:
    samples, days, players = a.health.shape
    limit = np.iinfo(np.int32).max
    values = {
        "samples": samples,
        "days": days,
        "players": players,
        "teams": teams,
        "week_count": a.week_count,
        "roster_capacity": a.roster_capacity,
        "slots": len(a.slots),
        "injury_slots": a.il_eligible.shape[-1],
        "add_limit": a.add_limit,
        "waiver_days": a.waiver_days,
    }
    for name, value in values.items():
        if (
            not isinstance(value, (int, np.integer))
            or isinstance(value, (bool, np.bool_))
            or not 0 <= value <= limit
        ):
            raise DataError(f"management.{name}: outside native integer range")
    # The current ABI also performs flattened offsets and release dates in signed int32.
    offsets = (
        days * players * max(1, a.il_eligible.shape[-1]),
        teams * a.roster_capacity,
        samples * teams * a.week_count * 3,
        days + samples,
        days + a.waiver_days,
        days * (a.roster_capacity + 1),
    )
    if any(value > limit for value in offsets):
        raise DataError("management: native integer range exceeded by offsets or rule arithmetic")
