from dataclasses import replace

import numpy as np
import pytest
from test_managed import reference_manager

from fba.adapters.native import NativeKernel
from fba.contracts.config import ManagementParameters
from fba.contracts.season import ManagementPolicy


@pytest.fixture(scope="module")
def kernel():
    native = NativeKernel()
    yield native
    native.close()


def management_parameters(manager, reserve):
    return ManagementParameters(
        long_forecast_days=28,
        candidate_limit=12,
        reserve_adds=reserve,
        minimum_gain=1e-9,
        opportunity_cost=0.25,
        evidence=manager.parameters.evidence,
    )


def oracle_history(manager, events, teams):
    histories = [[[] for _ in manager.weeks] for _ in range(teams)]
    for e in events:
        row = {"day": manager.days[e.day].isoformat(), "kind": e.kind}
        if e.kind == "lineup":
            row.update(
                active=[manager.ids[p] for p in e.active],
                il=[manager.ids[p] for p in e.injured],
                started=[manager.ids[p] for p in e.started],
            )
        else:
            row.update(
                drop=None if e.dropped is None else manager.ids[e.dropped],
                add=None if e.added is None else manager.ids[e.added],
            )
        histories[e.team][int(manager.week[e.day])].append(row)
    return histories


@pytest.mark.parametrize("case_index", range(12))
def test_all_reference_traces_adds_and_managed_moments(kernel, case_index):
    manager, spec, data = reference_manager(kernel, case_index)
    case = spec["case"]
    rosters = tuple(tuple(r) for r in case["rosters"])
    arrays = manager.arrays()
    if not case["il"]:
        arrays = replace(arrays, il_eligible=np.zeros_like(arrays.il_eligible))
    policy = ManagementPolicy(
        streaming_slots=tuple(case["flex"]), reserve_adds=case["reserve"], upgrades=case["upgrades"]
    )
    tactics = manager.tactics(policy, management_parameters(manager, case["reserve"]))
    result = kernel.run(arrays, rosters, manager.pool, tactics, True)
    primary = kernel.run(arrays, rosters, manager.pool, tactics, False, primary_only=True)
    np.testing.assert_array_equal(primary.counts, result.counts[:, :1])
    np.testing.assert_array_equal(primary.adds, result.adds)
    assert not primary.events
    assert oracle_history(manager, result.events, len(rosters)) == case["history"]
    np.testing.assert_array_equal(result.adds, data[f"{case_index}_adds"])
    np.testing.assert_array_equal(result.counts.sum(axis=-1), data[f"{case_index}_started"])
    np.testing.assert_allclose(
        result.counts @ manager.raw, data[f"{case_index}_physical_boxes"], rtol=1e-12, atol=1e-10
    )
    moments = manager.summarize(rosters, result.counts)
    for field, value in [
        ("mean", moments.mean),
        ("cov", moments.covariance),
        ("boxes", moments.boxes),
    ]:
        np.testing.assert_allclose(value, data[f"{case_index}_{field}"], rtol=1e-12, atol=1e-10)


@pytest.mark.parametrize("slots", [0, 1, 2, 3, 4])
def test_shared_add_reserve_and_known_out_candidates(kernel, slots):
    from test_managed import small_arrays

    from fba.contracts.season import TacticalArrays

    a = small_arrays()
    days, n = 7, 9
    a = replace(
        a,
        health=np.ones((1, days, n), dtype=np.uint8),
        games=np.ones((days, n), dtype=np.uint8),
        weeks=np.zeros(days, dtype=np.int32),
        periods=np.zeros(days, dtype=np.int32),
        masks=np.ones(n, dtype=np.uint64),
        priority=np.arange(n, dtype=float),
        value=np.arange(n, dtype=float),
        orders=np.tile(np.arange(n - 1, -1, -1, dtype=np.int32), (1, days, 1)),
        il_eligible=np.ones((days, n, 1), dtype=np.uint8),
        lock_days=np.array([1, 0, 0, 0, 0, 0, 0], dtype=np.uint8),
        roster_capacity=4,
        add_limit=4,
        waiver_days=0,
    )
    # Each day another free agent becomes better, while the strongest is publicly out.
    a.health[:, :, 8] = 0
    score = np.array(
        [[[float(i) + 20.0 * int(i == 4 + d % 4) for i in range(n)] for d in range(days)]]
    )
    score[:, :, 8] = 1000.0
    a = replace(a, orders=np.argsort(-score, axis=-1, kind="stable").astype(np.int32))
    tactics = TacticalArrays(
        ManagementPolicy(streaming_slots=(slots,), reserve_adds=1, upgrades=False),
        score,
        score,
        score,
        score,
        a.orders,
        a.orders,
        n,
        1e-9,
        0.25,
    )
    run = kernel.run(a, ((0, 1, 2, 3),), (4, 5, 6, 7, 8), tactics, True)
    assert run.adds.sum() <= 3
    streams = [e for e in run.events if e.kind == "stream"]
    assert bool(streams) == bool(slots)
    assert all(a.health[0, e.day, e.added] for e in streams)
    if slots:
        unreserved = kernel.run(
            a,
            ((0, 1, 2, 3),),
            (4, 5, 6, 7, 8),
            replace(tactics, policy=tactics.policy.model_copy(update={"reserve_adds": 0})),
            True,
        )
        assert unreserved.adds.sum() == 4  # the old k!=1 reserve path is observably rejected


def test_native_optional_trace_and_capacity_guards(kernel, monkeypatch):
    import ctypes

    from test_managed import small_arrays

    from fba.adapters.native import NativeOptions
    from fba.contracts.auction import SolverError
    from fba.contracts.season import TacticalArrays

    function = kernel.function

    def no_options(*args):
        args = list(args)
        args[-3] = None
        return function(*args)

    monkeypatch.setattr(kernel, "function", no_options)
    assert kernel(small_arrays(), ((0,),), (1, 2)).sum() == 3

    def no_capacity(*args):
        ctypes.cast(args[-3], ctypes.POINTER(NativeOptions)).contents.capacity = 0
        return function(*args)

    monkeypatch.setattr(kernel, "function", no_capacity)
    with pytest.raises(SolverError, match="trace capacity"):
        kernel.run(small_arrays(), ((0,),), (1, 2), None, True)
    monkeypatch.setattr(kernel, "function", function)
    a = small_arrays()
    a = replace(a, health=np.zeros_like(a.health))
    score = np.zeros(a.health.shape)
    tactics = TacticalArrays(
        ManagementPolicy(streaming_slots=(1,), reserve_adds=0, upgrades=True),
        score,
        score,
        score,
        score,
        a.orders,
        a.orders,
        3,
        1e-9,
        0.25,
    )
    assert not kernel.run(a, ((0,),), (), tactics, True).counts.any()


def test_changed_public_designation_releases_ineligible_injury_slot(kernel):
    from test_managed import small_arrays

    a = small_arrays()
    a.health[:, :, 0] = 0
    a.il_eligible[1:, 0] = 0
    result = kernel.run(a, ((0,),), (1, 2), None, True)
    assert result.counts.ravel().tolist() == [0, 1, 0]
    assert all(not e.injured for e in result.events if e.kind == "lineup" and e.day >= 1)


def test_injury_designation_transfers_without_releasing_replacement_or_spending_add(kernel):
    from test_managed import small_arrays

    a = small_arrays()
    eligible = np.ones((3, 3, 2), dtype=np.uint8)
    eligible[1, 0, 0] = 0
    a = replace(a, il_eligible=eligible, add_limit=2)
    a.health[0, 1, 0] = 0
    result = kernel.run(a, ((0,),), (1, 2), None, True)
    assert result.adds.sum() == 1
    assert not [e for e in result.events if e.day == 1 and e.kind != "lineup"]
    lineup = next(e for e in result.events if e.day == 1 and e.kind == "lineup")
    assert lineup.active == (1,)
    assert lineup.injured == (0,)
    assert lineup.started == (1,)


def test_injury_transfer_can_reassign_an_occupied_flexible_slot(kernel):
    from test_managed import small_arrays

    a = small_arrays()
    health = np.ones((1, 3, 4), dtype=np.uint8)
    health[0, :2, :2] = 0
    eligible = np.ones((3, 4, 2), dtype=np.uint8)
    eligible[1, 0, 0] = 0  # first player now requires the second injury slot
    a = replace(
        a,
        health=health,
        games=np.ones((3, 4), dtype=np.uint8),
        masks=np.ones(4, dtype=np.uint64),
        priority=np.array([4.0, 3.0, 2.0, 1.0]),
        value=np.array([4.0, 3.0, 2.0, 1.0]),
        orders=np.tile(np.arange(4, dtype=np.int32), (1, 3, 1)),
        il_eligible=eligible,
        roster_capacity=2,
        add_limit=4,
    )
    result = kernel.run(a, ((0, 1),), (2, 3), None, True)
    assert result.adds.sum() == 2
    assert not [e for e in result.events if e.day == 1 and e.kind != "lineup"]
    lineup = next(e for e in result.events if e.day == 1 and e.kind == "lineup")
    assert set(lineup.active) == {2, 3}
    assert set(lineup.injured) == {0, 1}


@pytest.mark.parametrize("changed_day", [0, 1])
def test_injury_slot_matching_handles_every_three_player_assignment(kernel, changed_day):
    from itertools import permutations

    from test_managed import small_arrays

    health = np.ones((1, 3, 6), dtype=np.uint8)
    health[0, :2, :3] = 0
    for assignment in permutations(range(3)):
        eligible = np.ones((3, 6, 3), dtype=np.uint8)
        eligible[changed_day, :3] = 0
        eligible[changed_day, np.arange(3), assignment] = 1
        a = replace(
            small_arrays(),
            health=health,
            games=np.ones((3, 6), dtype=np.uint8),
            masks=np.ones(6, dtype=np.uint64),
            priority=np.arange(6, 0, -1, dtype=float),
            value=np.arange(6, 0, -1, dtype=float),
            orders=np.tile(np.arange(6, dtype=np.int32), (1, 3, 1)),
            il_eligible=eligible,
            roster_capacity=3,
            add_limit=6,
        )
        result = kernel.run(a, ((0, 1, 2),), (3, 4, 5), None, True)
        assert result.adds.sum() == 3
        assert not [e for e in result.events if e.day == 1 and e.kind != "lineup"]
        assert all(
            set(e.injured) == {0, 1, 2} for e in result.events if e.kind == "lineup" and e.day < 2
        )


def test_shared_pool_candidate_limit_preserves_unsorted_input_order(kernel):
    from test_managed import small_arrays

    from fba.contracts.season import TacticalArrays

    a = replace(
        small_arrays(),
        health=np.ones((1, 1, 5), dtype=np.uint8),
        games=np.ones((1, 5), dtype=np.uint8),
        weeks=np.zeros(1, dtype=np.int32),
        periods=np.zeros(1, dtype=np.int32),
        masks=np.ones(5, dtype=np.uint64),
        priority=np.ones(5),
        value=np.ones(5),
        orders=np.array([[[2, 3, 4, 0, 1]]], dtype=np.int32),
        il_eligible=np.zeros((1, 5, 0), dtype=np.uint8),
        lock_days=np.ones(1, dtype=np.uint8),
    )
    values = np.array([[[0.0, 0.0, 3.0, 2.0, 100.0]]])
    tactics = TacticalArrays(
        ManagementPolicy(streaming_slots=(1, 1), reserve_adds=0, upgrades=False),
        values,
        values,
        values,
        values,
        a.orders,
        a.orders,
        1,
        0.0,
        0.0,
    )
    result = kernel.run(a, ((0,), (1,)), (2, 3, 4), tactics, True)
    assert [(e.team, e.added) for e in result.events if e.kind == "stream"] == [(0, 2), (1, 3)]


@pytest.mark.parametrize("fault", ["known_out", "replacement_chain"])
def test_invariants_detect_old_fault_classes_in_native_kernel(tmp_path, monkeypatch, fault):
    import subprocess
    import tempfile
    from pathlib import Path

    from test_managed import small_arrays

    import fba.adapters.native as native
    from fba.contracts.auction import SolverError
    from fba.contracts.season import TacticalArrays

    source = (Path(__file__).parents[1] / "src/fba/native/season.cpp").read_text()
    before, after = {
        "known_out": (
            "if (!today[p] || !free[p] || release[p] > current_day) continue;",
            "if (!free[p] || release[p] > current_day) continue;",
        ),
        "replacement_chain": ("team.active.erase(same);", "(void)same;"),
    }[fault]
    assert source.count(before) == 1
    changed = tmp_path / "mutant.cpp"
    changed.write_text(source.replace(before, after))
    library = tmp_path / "mutant.so"
    subprocess.run(
        [
            native.compiler_path(),
            "-std=c++17",
            "-O2",
            "-ffp-contract=off",
            "-fPIC",
            "-shared",
            str(changed),
            "-o",
            str(library),
        ],
        check=True,
        capture_output=True,
    )
    monkeypatch.setattr(
        native,
        "compile_kernel",
        lambda: (tempfile.TemporaryDirectory(prefix="test-fault-"), library),
    )
    broken = NativeKernel()
    try:
        a = small_arrays()
        if fault == "replacement_chain":
            a.health[0, 1, 1] = 0
            a = replace(a, il_eligible=np.ones((3, 3, 2), dtype=np.uint8), add_limit=2)
            with pytest.raises(SolverError, match="capacity|ownership|Duplicate"):
                broken.run(a, ((0,),), (1, 2), None, True)
        else:
            a = replace(
                a,
                health=np.ones_like(a.health),
                il_eligible=np.zeros_like(a.il_eligible),
                add_limit=1,
            )
            a.health[:, :, 2] = 0
            score = np.broadcast_to(np.array([1.0, 2.0, 100.0]), a.health.shape).copy()
            a = replace(a, orders=np.argsort(-score, axis=-1).astype(np.int32))
            tactics = TacticalArrays(
                ManagementPolicy(streaming_slots=(1,), reserve_adds=0, upgrades=False),
                score,
                score,
                score,
                score,
                a.orders,
                a.orders,
                3,
                1e-9,
                0.25,
            )
            result = broken.run(a, ((0,),), (1, 2), tactics, True)
            assert any(
                e.kind == "stream" and not a.health[0, e.day, e.added] for e in result.events
            )
    finally:
        broken.close()
