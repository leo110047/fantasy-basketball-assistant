"""Actual F3 search must recover the previously enumerated counterexamples.

Reference maxima use exchangeable_count_v1 and come from independently enumerated
legal plans, including excluded drops and non-improving prefixes:
75/60 one-add plans and 2,900/2,510 two-add plans. The previous game-ID objective
had different maxima; sample counts, enumeration coverage and tolerance remain.
These fixed model scores are synthetic regression evidence, not historical
recall or a claim about the accuracy of the projections.
"""

import pytest
from inseason_search_support import search_scenario
from inseason_support import DEFAULTS, simulation

from fba.contracts.inseason import CalculationTimeout, InseasonPreferences
from fba.inseason.recommendations import search_add_plans


@pytest.mark.parametrize(
    "mode,seed,adds,maximum,count",
    [
        ("h2h_one_win", 0, 1, 0.8512, 75),
        ("h2h_each_category", 3, 1, 2.0869, 60),
        ("h2h_one_win", 0, 2, 1.356, 2900),
        ("h2h_each_category", 2, 2, 4.40832, 2510),
    ],
)
def test_search_recovers_best_legal_plan(monkeypatch, mode, seed, adds, maximum, count):
    from fba.inseason import recommendations

    sim, prefs = search_scenario(seed, mode, adds)
    # Complete engine quality is separate from the product's unchanged time
    # budget. Deadline/cancellation and refusal to publish partial results
    # are tested below; this offline enumeration may exceed that budget.
    # Freeze only the budget clock: nested week calls otherwise turn this
    # quality assertion into a machine-speed gate on slower CI runners.
    monkeypatch.setattr("fba.inseason.matchup.monotonic", lambda: 0.0)
    original = recommendations.evaluate_plan
    evaluated = 0

    def record(*args, **kwargs):
        nonlocal evaluated
        evaluated += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(recommendations, "evaluate_plan", record)
    plans = search_add_plans(sim, prefs, "2", lambda _: None)
    assert plans
    assert plans[0].score == pytest.approx(maximum, abs=sim.params.tolerance.value, rel=0)
    assert evaluated == count
    assert plans[0].after.sample_coupling[sim.snapshot.mine] == "exchangeable_count_v1"


@pytest.mark.parametrize("interruption", ["deadline", "cancelled"])
def test_interruption_after_priority_batch_never_saves_a_partial_plan(monkeypatch, interruption):
    from types import SimpleNamespace

    from fba.inseason import matchup, operations, recommendations

    sim = simulation()
    sim.league = sim.league.model_copy(update={"adds_per_week": 1})
    prefs = InseasonPreferences.model_validate_json((DEFAULTS / "preferences.json").read_bytes())
    prefs = prefs.model_copy(update={"reserve_adds": 0})
    clock, cancelled = [0.0], [False]
    monkeypatch.setattr(matchup, "monotonic", lambda: clock[0])
    sim.cancelled = lambda: cancelled[0]
    original = recommendations.evaluate_plan
    evaluated, saved = [], []

    def interrupt(*args, **kwargs):
        plan = original(*args, **kwargs)
        evaluated.append(plan)
        if len(evaluated) == sim.params.shortlist.value + 1:
            if interruption == "deadline":
                clock[0] = sim.params.budgets["recommendations"].value + 1
            else:
                cancelled[0] = True
        return plan

    monkeypatch.setattr(recommendations, "evaluate_plan", interrupt)
    monkeypatch.setattr(operations, "record_forecast", lambda *args: saved.append(args))
    session = SimpleNamespace(simulation=lambda: sim, preferences=prefs, progress=0.0)
    with pytest.raises(CalculationTimeout, match="time budget|cancelled"):
        operations.recommendations(session, "2")
    assert len(evaluated) > sim.params.shortlist.value
    assert any(recommendations.admissible_plan(p, sim.params.tolerance.value) for p in evaluated)
    assert not saved


def test_search_visits_all_legal_plans_and_returns_bounded_ranked_choices(monkeypatch):
    from fba.inseason import recommendations, replay_oracle

    sim = simulation()
    sim.league = sim.league.model_copy(update={"adds_per_week": 2})
    prefs = InseasonPreferences.model_validate_json((DEFAULTS / "preferences.json").read_bytes())
    prefs = prefs.model_copy(update={"reserve_adds": 0})
    before = sim.week("team0", "team1", "2")
    _, future = recommendations.search_context(sim, before)
    visited, reference, progress = [], [], []
    original = recommendations.evaluate_plan

    def record(target):
        def evaluate(*args, **kwargs):
            plan = original(*args, **kwargs)
            target.append(plan)
            return plan

        return evaluate

    monkeypatch.setattr(recommendations, "evaluate_plan", record(visited))
    plans = search_add_plans(sim, prefs, "2", progress.append)
    monkeypatch.setattr(replay_oracle, "evaluate_plan", record(reference))
    count, retained = replay_oracle.exhaustive_plans(
        sim, prefs, before, future, 2, 1000, plans[0].score
    )
    assert count == len(visited) == len({p.id for p in visited})
    assert {p.id for p in visited} == {p.id for p in reference}
    assert retained is True
    assert progress == sorted(progress) and progress[-1] == 1.0
    expected = []
    for depth in (1, 2):
        legal = [
            p
            for p in reference
            if len(p.moves) == depth
            and recommendations.admissible_plan(p, sim.params.tolerance.value)
        ]
        legal.sort(key=lambda p: (-round(p.score / sim.params.tolerance.value), p.id))
        expected.extend(legal[: sim.params.shortlist.value])
    expected.sort(key=lambda p: (-round(p.score / sim.params.tolerance.value), p.id))
    assert [p.id for p in plans] == [p.id for p in expected]
