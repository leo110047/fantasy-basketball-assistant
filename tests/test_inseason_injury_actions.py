"""Conditional return plans and Today activation instructions must agree."""

from datetime import timedelta

import pytest
from test_inseason_priority import healthy_il_simulation

from fba.inseason.matchup import Simulation
from fba.inseason.today import today


def rebuild_at(sim, days_later):
    rebuilt = Simulation(
        sim.league,
        sim.params,
        sim.players,
        sim.priors,
        sim.ledger,
        sim.snapshot,
        sim.as_of + timedelta(days=days_later),
        sim.samples,
    )
    week = next(w for w in rebuilt.league.matchups if w.start <= rebuilt.as_of.date() <= w.end)
    if week.id != "2":
        rebuilt.snapshot = rebuilt.snapshot.model_copy(
            update={
                "actual": tuple(
                    s.model_copy(update={"week_id": week.id, "through": rebuilt.as_of})
                    for s in rebuilt.snapshot.actual
                )
            }
        )
    return rebuilt


def test_estimated_il_return_requires_activation_action_even_while_provider_says_injured():
    sim = healthy_il_simulation()
    on = sim.as_of.astimezone(sim.zone).date() + timedelta(days=1)
    sim.players = sim.players.model_copy(
        update={
            "players": tuple(
                p.model_copy(update={"status": "INJ", "return_on": on}) if p.id == "p6" else p
                for p in sim.players.players
            )
        }
    )
    forecast = sim.week("team0", "team1", "2")
    returning = next(m for m in forecast.injury_returns if m.player_id == "p6")
    assert returning.effective_on == on and returning.estimated
    before = today(sim, on - timedelta(days=1), "Asia/Taipei")
    assert not any(a.kind == "injury_out" and a.player_id == "p6" for a in before.actions)
    result = today(sim, on, "Asia/Taipei")
    activation = next(a for a in result.actions if a.kind == "injury_out" and a.player_id == "p6")
    assert "預估回歸情境" in activation.reason and "確認實際狀態" in activation.reason
    assert f"釋出 {returning.drop}" in activation.reason
    assert any(a.kind == "drop" and a.player_id == returning.drop for a in result.actions)
    assert "p6" in (*result.lineup.slots.values(), *result.lineup.bench)
    assert returning.drop not in (*result.lineup.slots.values(), *result.lineup.bench)
    completed = today(sim, on, "Asia/Taipei", completed=(activation.id,))
    assert next(a for a in completed.actions if a.id == activation.id).completed


@pytest.mark.parametrize("days_later", (1, 2))
@pytest.mark.parametrize("space", (False, True))
def test_unconfirmed_estimated_return_stays_pending_after_midnight(days_later, space, monkeypatch):
    sim = healthy_il_simulation()
    on = sim.as_of.astimezone(sim.zone).date() + timedelta(days=1)
    sim.players = sim.players.model_copy(
        update={
            "players": tuple(
                p.model_copy(update={"status": "INJ", "return_on": on}) if p.id == "p6" else p
                for p in sim.players.players
            )
        }
    )
    if space:
        sim.snapshot = sim.snapshot.model_copy(
            update={
                "teams": tuple(
                    t.model_copy(
                        update={
                            "players": ("p0", "p1"),
                            "selected_slots": {"guard": "p0"},
                        }
                    )
                    if t.id == "team0"
                    else t
                    for t in sim.snapshot.teams
                )
            }
        )
    assert sim.week("team0", "team1", "2").injury_returns
    rebuilt = rebuild_at(sim, days_later)

    def unexpected(*args, **kwargs):
        raise AssertionError("Today must not repeat a season/drop search after midnight")

    monkeypatch.setattr("fba.inseason.injury_returns.return_plan", unexpected)
    result = today(rebuilt, rebuilt.as_of.astimezone(rebuilt.zone).date(), "Asia/Taipei")
    assert result.injury_pending == ("p6",)
    assert not result.week_forecast.injury_returns
    assert not any(a.kind in ("drop", "injury_out") for a in result.actions)
    assert "p6" not in (*result.lineup.slots.values(), *result.lineup.bench)
    assert next(p for p in rebuilt.projection(on).players if p.player.id == "p6").probability == 0


def test_latest_injured_source_cancels_the_overdue_return_assumption():
    sim = healthy_il_simulation()
    on = sim.as_of.astimezone(sim.zone).date()
    sim.players = sim.players.model_copy(
        update={
            "players": tuple(
                p.model_copy(update={"status": "INJ", "return_on": on, "known_at": sim.as_of})
                if p.id == "p6"
                else p
                for p in sim.players.players
            )
        }
    )
    result = today(sim, on, "Asia/Taipei")
    assert not result.injury_pending and not result.week_forecast.injury_returns
    assert not any(a.kind in ("drop", "injury_out") for a in result.actions)


def test_session_midnight_invalidation_keeps_an_unconfirmed_return_visible(tmp_path):
    from test_inseason_acceptance_gaps import prepared_session

    from fba.inseason.session import json_value

    sim = healthy_il_simulation()
    session, now = prepared_session(tmp_path)
    on = now.astimezone(sim.zone).date() + timedelta(days=1)
    players = sim.players.model_copy(
        update={
            "players": tuple(
                p.model_copy(update={"status": "INJ", "return_on": on}) if p.id == "p6" else p
                for p in sim.players.players
            )
        }
    )
    store = session.league_store()
    hashes = {
        name: store.snapshot("IL test", now, json_value(value.model_dump(mode="json")))
        for name, value in (("players_sha256", players), ("normalized_sha256", sim.snapshot))
    }
    session.save_state(session.state().model_copy(update={"league": sim.league, **hashes}))
    previous = session.simulation(as_of=now, require_league=False)
    assert previous.week("team0", "team1", "2").injury_returns
    current = session.simulation(as_of=now + timedelta(days=1), require_league=False)
    assert not current.injury_plan_cache and not current.forecast_cache
    result = today(current, on, "Asia/Taipei")
    assert result.injury_pending == ("p6",)
    assert not result.week_forecast.injury_returns
