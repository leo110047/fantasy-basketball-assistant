import json
from pathlib import Path
from time import perf_counter

from test_auction import inputs_for, state

from fba.adapters.codec import canonical, digest
from fba.apps.auction import AuctionSession
from fba.contracts.auction import AuctionPlayer, Plan, Sale
from fba.core.auction import calculate_auction


def test_twenty_fresh_draft_states_preserve_exact_results_and_performance():
    root = Path(__file__).parent / "fixtures"
    replay = json.loads((root / "auction-replay.json").read_text())
    reference = json.loads((root / replay["reference"]).read_text())
    players = tuple(AuctionPlayer.model_validate_json(json.dumps(p)) for p in reference["players"])
    inputs = inputs_for(players)
    draft = state(inputs)
    session = AuctionSession(replay["workers"])
    durations = []
    hashes = set()
    try:
        # Process startup is measured separately in the full acceptance replay.
        calculate_auction(inputs, draft, "0" * 64, digest(canonical(draft)), runner=session.caps)
        for item in replay["sales"]:
            draft = draft.model_copy(
                update={
                    "sales": (*draft.sales, Sale.model_validate(item)),
                    "revision": draft.revision + 1,
                }
            )
            state_hash = digest(canonical(draft))
            assert state_hash not in hashes
            hashes.add(state_hash)
            start = perf_counter()
            fast = calculate_auction(inputs, draft, "0" * 64, state_hash, runner=session.caps)
            durations.append(perf_counter() - start)
            slow = calculate_auction(inputs, draft, "0" * 64, state_hash)
            assert canonical(fast) == canonical(slow)
            assert isinstance(fast.plan, Plan)
        p95 = sorted(durations)[18]
        print(json.dumps({"p95": p95, "maximum": max(durations), "samples": len(durations)}))
        assert p95 <= replay["baseline_p95_seconds"] * (1 + replay["allowed_regression"])
    finally:
        session.close()
