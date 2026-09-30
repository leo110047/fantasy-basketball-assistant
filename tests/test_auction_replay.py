import json
from math import ceil
from pathlib import Path
from time import perf_counter

import pytest
from test_auction import inputs_for, state

from fba.apps.auction import AuctionSession
from fba.auction.auction import calculate_auction, run_caps
from fba.contracts.auction import AuctionPlayer, Plan, Sale
from fba.data.codec import canonical, digest


def relative_performance(fast, reference, allowance):
    assert len(fast) == len(reference) and fast
    rank = ceil(len(fast) * 0.95) - 1
    fast_p95, reference_p95 = sorted(fast)[rank], sorted(reference)[rank]
    print(
        json.dumps(
            {
                "fast_p95": fast_p95,
                "reference_p95": reference_p95,
                "fast_maximum": max(fast),
                "reference_maximum": max(reference),
                "samples": len(fast),
            }
        )
    )
    assert fast_p95 <= reference_p95 * (1 + allowance)


def test_relative_gate_is_portable_and_rejects_slowdown():
    # The same relative result must pass on fast and slow hosts; a 20% regression must fail.
    for seconds in (0.01, 10):
        reference = [seconds] * 20
        relative_performance([seconds * 1.05] * 20, reference, 0.1)
        with pytest.raises(AssertionError):
            relative_performance([seconds * 1.2] * 20, reference, 0.1)


def test_twenty_fresh_draft_states_preserve_exact_results_and_performance():
    root = Path(__file__).parent / "fixtures"
    replay = json.loads((root / "auction-replay.json").read_text())
    reference = json.loads((root / replay["reference"]).read_text())
    players = tuple(AuctionPlayer.model_validate_json(json.dumps(p)) for p in reference["players"])
    inputs = inputs_for(players)
    draft = state(inputs)
    session = AuctionSession(replay["workers"])
    durations = {"fast": [], "reference": []}
    hashes = set()
    try:
        # Process startup is measured separately in the full acceptance replay.
        calculate_auction(inputs, draft, "0" * 64, digest(canonical(draft)), runner=session.caps)
        calculate_auction(inputs, draft, "0" * 64, digest(canonical(draft)))
        for index, item in enumerate(replay["sales"]):
            draft = draft.model_copy(
                update={
                    "sales": (*draft.sales, Sale.model_validate(item)),
                    "revision": draft.revision + 1,
                }
            )
            state_hash = digest(canonical(draft))
            assert state_hash not in hashes
            hashes.add(state_hash)
            # Alternate ordering to avoid always giving one path the warmer CPU/cache state.
            results = {}
            for name in ("fast", "reference") if index % 2 else ("reference", "fast"):
                start = perf_counter()
                results[name] = calculate_auction(
                    inputs,
                    draft,
                    "0" * 64,
                    state_hash,
                    runner=session.caps if name == "fast" else run_caps,
                )
                durations[name].append(perf_counter() - start)
            assert canonical(results["fast"]) == canonical(results["reference"])
            assert isinstance(results["fast"].plan, Plan)
        relative_performance(
            durations["fast"], durations["reference"], replay["allowed_regression"]
        )
    finally:
        session.close()
