from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from pydantic import JsonValue, TypeAdapter

from fba.contracts.base import DataError
from fba.contracts.inseason import AdjustmentLedger, InseasonPreferences
from fba.contracts.inseason_results import AddPlan, PredictionRecord, WeekForecast
from fba.data.codec import canonical, digest

if TYPE_CHECKING:
    from fba.inseason.session import InseasonSession


def recommendation_policy(preferences: InseasonPreferences) -> str:
    return digest(
        json.dumps(
            {
                "future_weight": preferences.future_weight,
                "reserve_adds": preferences.reserve_adds,
                "untouchable": sorted(preferences.untouchable),
            },
            sort_keys=True,
            allow_nan=False,
        ).encode()
    )


def record_forecast(
    session: InseasonSession, week_id: str, plans: tuple[AddPlan, ...] = ()
) -> WeekForecast:
    sim = session.simulation()
    team = sim.snapshot.mine
    pairing = next(
        (p for p in sim.snapshot.pairings if p.week_id == week_id and team in (p.home, p.away)),
        None,
    )
    if pairing is None:
        raise DataError(f"matchup.{week_id}: no actual opponent for this team")
    opponent = pairing.away if pairing.home == team else pairing.home
    week = next(w for w in sim.league.matchups if w.id == week_id)
    if week.end < sim.as_of.astimezone(sim.zone).date():
        raise DataError(
            "predictions: completed weeks belong in the review; "
            "cannot create a retrospective forecast"
        )
    with sim.budget("week"):
        result = sim.week(team, opponent, week_id)
        if sim.ledger.entries:
            baseline_sim = session.simulation(
                ledger=AdjustmentLedger(format_version=1, entries=()), as_of=sim.as_of
            )
            baseline_sim.deadline = sim.deadline
            try:
                baseline = baseline_sim.week(team, opponent, week_id)
            finally:
                baseline_sim.deadline = None
        else:
            baseline = result
    state = session.state()
    identity = {
        "week": week_id,
        "inputs": [state.normalized_sha256, state.players_sha256, state.priors_sha256],
        "parameters": digest(canonical(session.params)),
        "league": digest(canonical(sim.league)),
        "ledger": digest(canonical(session.ledger())),
        "forecast": result.model_dump(mode="json"),
        "plans": [p.model_dump(mode="json") for p in plans],
        "recommendation_policy": recommendation_policy(session.preferences) if plans else None,
    }
    record = PredictionRecord(
        id=digest(
            json.dumps(
                identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            ).encode()
        ),
        created_at=sim.as_of,
        week_id=week_id,
        parameter_version=session.params.version,
        parameter_sha256=digest(canonical(session.params)),
        input_hashes=tuple(
            s
            for s in (state.normalized_sha256, state.players_sha256, state.priors_sha256)
            if s is not None
        ),
        ledger_sha256=digest(canonical(session.ledger())),
        league_sha256=digest(canonical(sim.league)),
        recommendation_policy_sha256=recommendation_policy(session.preferences) if plans else None,
        with_adjustments=result,
        without_adjustments=baseline,
        recommendations=plans,
        proposal_probabilities={},
        week_score_kind="win_probability",
    )
    store = session.league_store()
    # The ID is content/decision-state based, whereas created_at is the first
    # actual recording time. Reloading an unchanged page preserves that origin.
    if store.find_snapshot("predictions", record.id) is not None:
        return result
    store.append_snapshot(
        "predictions",
        "inseason forecast",
        sim.as_of,
        TypeAdapter[JsonValue](JsonValue).validate_python(record.model_dump(mode="json")),
        identity=record.id,
    )
    return result


def record_current_forecast(session: InseasonSession) -> None:
    now = datetime.now(UTC)
    state = session.state()
    if state.league is None or not session.feature_availability(now).enabled:
        return
    on = now.astimezone(ZoneInfo(state.league.timezone)).date()
    current = next((w for w in state.league.matchups if w.start <= on <= w.end), None)
    if current is not None:
        record_forecast(session, current.id)
