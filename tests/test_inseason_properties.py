from datetime import timedelta

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from inseason_support import DEFAULTS, simulation

from fba.contracts.config import StarterSlot
from fba.contracts.inseason import InseasonPreferences
from fba.core.lineups import legal_assignment
from fba.inseason.recommendations import earliest_move, legal_roster, search_adds


@settings(max_examples=24, deadline=None)
@example(
    adds=2,
    used=0,
    reserve=0,
    next_day=False,
    flexible=False,
    waiver_delay=0,
    mode="h2h_one_win",
    free_positions=[("PG",), ("C",), ("PG", "C")],
)
@given(
    adds=st.integers(0, 3),
    used=st.integers(0, 3),
    reserve=st.integers(0, 3),
    next_day=st.booleans(),
    flexible=st.booleans(),
    waiver_delay=st.integers(0, 3),
    mode=st.sampled_from(("h2h_one_win", "h2h_each_category")),
    free_positions=st.lists(
        st.sampled_from((("PG",), ("C",), ("PG", "C"))), min_size=3, max_size=3
    ),
)
def test_random_rules_and_candidate_positions_preserve_every_plan_constraint(
    adds, used, reserve, next_day, flexible, waiver_delay, mode, free_positions
):
    sim = simulation(mode=mode)
    slots = sim.league.starter_slots
    if flexible:
        slots = tuple(
            StarterSlot(id=s.id, label=s.label, eligible_positions=("PG", "C")) for s in slots
        )
    sim.league = sim.league.model_copy(
        update={
            "adds_per_week": adds,
            "effective": "next_day" if next_day else "same_day",
            "starter_slots": slots,
            "waiver_days": waiver_delay,
        }
    )
    free = {
        f.player_id: positions
        for f, positions in zip(sim.snapshot.free_agents, free_positions, strict=True)
    }
    sim.players = sim.players.model_copy(
        update={
            "players": tuple(
                p.model_copy(update={"positions": free[p.id]}) if p.id in free else p
                for p in sim.players.players
            )
        }
    )
    sim.snapshot = sim.snapshot.model_copy(
        update={
            "teams": tuple(
                t.model_copy(update={"adds_used": used}) if t.id == sim.snapshot.mine else t
                for t in sim.snapshot.teams
            ),
            "free_agents": tuple(
                f.model_copy(
                    update={
                        "status": "waiver",
                        "clears_at": sim.as_of + timedelta(days=waiver_delay),
                    }
                )
                for f in sim.snapshot.free_agents
            ),
        }
    )
    prefs = InseasonPreferences.model_validate_json((DEFAULTS / "preferences.json").read_bytes())
    prefs = prefs.model_copy(update={"reserve_adds": reserve, "untouchable": ("p0",)})
    plans = search_adds(sim, prefs, "2", lambda _: None)
    by_id = {p.id: p for p in sim.players.players}
    for plan in plans:
        assert len(plan.moves) <= max(0, adds - used - reserve)
        roster = sim.roster(sim.snapshot.mine)
        previous = earliest_move(sim)
        for move in plan.moves:
            assert move.effective_on >= previous
            assert move.drop != "p0" and move.drop in roster and move.add not in roster
            roster = tuple(p for p in roster if p != move.drop) + (move.add,)
            assert legal_roster(sim, roster, move.effective_on)
            previous = move.effective_on
        for day in plan.after.lineups:
            ids = tuple(day.slots.values())
            assert legal_assignment(slots, {p: by_id[p].positions for p in ids}, ids) is not None
        assert all(0 <= c.probability <= 1 for c in plan.after.categories)
        assert plan.score == pytest.approx(
            plan.delta_week + prefs.future_weight * plan.delta_season
        )
