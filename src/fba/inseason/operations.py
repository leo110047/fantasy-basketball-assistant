from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import JsonValue

from fba.contracts.base import DataError
from fba.contracts.inseason import AdjustmentEntry, AdjustmentLedger, LeagueSnapshot, Proposal
from fba.contracts.inseason_app import AdjustmentsRequest, ProposalRequest, RevokeRequest
from fba.contracts.inseason_backtest import BacktestReport
from fba.contracts.inseason_results import AddPlan, PredictionRecord, WeekForecast, WeeklyReview
from fba.core.inseason import validate_inseason, validate_ledger
from fba.core.proposals import latest_proposals
from fba.data.codec import canonical, decode, digest
from fba.formulas.fitting import fit_acceptance
from fba.formulas.registry import definitions
from fba.inseason.adjustments import entry_state, redistribute
from fba.inseason.projection import visible_games
from fba.inseason.recommendations import search_adds
from fba.inseason.review import calibration_bins, cumulative_review, latest_reviews, weekly_review
from fba.inseason.session import InseasonSession, json_value, snapshot_record
from fba.inseason.team_view import team_views
from fba.inseason.trades import evaluate_trade


def bootstrap(session: InseasonSession) -> JsonValue:
    now = datetime.now(UTC)
    result: dict[str, JsonValue] = {
        "preferences": json_value(session.preferences.model_dump(mode="json")),
        "parameters": json_value(session.params.model_dump(mode="json")),
        "selected": session.preferences.selected_league,
        "leagues": json_value([d.model_dump(mode="json") for d in session.discovered]),
        "phase": session.phase,
        "progress": session.progress,
        "error": session.last_error,
        "formulas": [f.model_dump(mode="json") for f in definitions()],
        "credentials": json_value(session.auth.status()),
        "next_sync_at": session.next_sync_at.isoformat() if session.next_sync_at else None,
    }
    if session.preferences.selected_league is None:
        result.update(
            {
                "availability": {
                    "enabled": False,
                    "reason": "尚未連結 Yahoo",
                    "repair": "輸入自己的 API 憑證並完成授權",
                    "as_of": None,
                },
                "state": None,
            }
        )
        result.update(player_bootstrap(session, now))
        return result
    state, store = session.state(), session.league_store()
    status = session.feature_availability(now)
    result["availability"] = json_value(status.model_dump(mode="json"))
    result["state"] = json_value(state.model_dump(mode="json"))
    result["unresolved_free"] = (
        list(
            snapshot_record(
                store.load_snapshot(state.normalized_sha256), LeagueSnapshot
            ).unresolved_free
        )
        if state.normalized_sha256
        else []
    )
    result["sync_log"] = [
        {**row.payload, "at": row.as_of.isoformat()}
        for row in reversed(store.history("sync-log"))
        if isinstance(row.payload, dict)
    ]
    result["differences"] = (
        session.setting_differences(state, state.draft.document, state.draft.yahoo_settings)
        if state.draft
        else []
    )
    result.update(player_bootstrap(session, now))
    result["snapshot"] = (
        store.load_snapshot(state.normalized_sha256).payload
        if state.normalized_sha256 and status.enabled
        else None
    )
    reviews = store.history("reviews")
    current_reviews = latest_reviews(tuple(snapshot_record(row, WeeklyReview) for row in reviews))
    result["reports"] = [row.model_dump(mode="json") for row in reversed(current_reviews)]
    result["review_versions"] = len(reviews)
    result["cumulative_review"] = cumulative_review(
        tuple(snapshot_record(row, WeeklyReview) for row in reviews), session.params
    )
    result["proposals"] = [
        row.model_dump(mode="json")
        for row in reversed(
            latest_proposals(
                tuple(snapshot_record(s, Proposal) for s in store.history("proposals"))
            )
        )
    ]
    result["calibration"] = calibration_status(session)
    return result


def player_bootstrap(session: InseasonSession, now: datetime) -> dict[str, JsonValue]:
    ledger = session.ledger()
    result: dict[str, JsonValue] = {
        "ledger_sha256": digest(canonical(ledger)),
        "notes": json_value(session.notes().model_dump(mode="json")),
    }
    on = now.date()
    workspace = session.workspace()
    result["workspace"] = json_value(workspace.model_dump(mode="json")) if workspace else None
    try:
        rules, players, _ = session.player_inputs()
        projection = session.player_projection(now)
        on = projection.on
        result["projection"] = json_value(projection.model_dump(mode="json"))
        result["projection_rules"] = json_value(rules.model_dump(mode="json"))
        result["player_source"] = {"as_of": players.as_of.isoformat(), "source": players.source}
        result["schedule"] = json_value(
            [g.model_dump(mode="json") for g in visible_games(players, now)]
        )
        league = session.state().league if session.preferences.selected_league else None
        result["team_views"] = [
            row.model_dump(mode="json")
            for row in team_views(
                projection, rules, session.params, visible_games(players, now), league, ledger
            )
        ]
    except DataError as exc:
        result["projection_error"] = str(exc)
    result["ledger"] = json_value(
        [
            {**e.model_dump(mode="json"), "state": entry_state(e, ledger, on, now)}
            for e in ledger.entries
        ]
    )
    return result


def calibration_status(session: InseasonSession) -> JsonValue:
    path = session.league_store().root / "validation.json"
    if not path.exists():
        return {"enabled": False, "reason": "尚無樣本外回測報告；建議未校準，尚未通過交付驗收"}
    report = decode(BacktestReport, path.read_bytes(), str(path))
    matches = report.parameters_sha256 == digest(canonical(session.params))
    return {
        "enabled": report.passed and matches,
        "reason": "已通過" if matches and report.passed else "回測尚未通過全部關卡或參數版本不同",
        "report": json_value(report.model_dump(mode="json")),
    }


def week_result(
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
    result = sim.week(team, opponent, week_id)
    baseline = session.simulation(
        ledger=AdjustmentLedger(format_version=1, entries=()), as_of=sim.as_of
    ).week(team, opponent, week_id)
    state = session.state()
    record = PredictionRecord(
        id=str(uuid4()),
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
        with_adjustments=result,
        without_adjustments=baseline,
        recommendations=plans,
        proposal_probabilities={},
        week_score_kind="win_probability",
    )
    session.league_store().append_snapshot(
        "predictions", "inseason forecast", sim.as_of, json_value(record.model_dump(mode="json"))
    )
    return result


def recommendations(session: InseasonSession, week_id: str) -> tuple[AddPlan, ...]:
    def progress(value: float) -> None:
        session.progress = value

    result = search_adds(session.simulation(), session.preferences, week_id, progress)
    week_result(session, week_id, result)
    return result


def adjustment_entries(
    session: InseasonSession, request: AdjustmentsRequest, now: datetime
) -> AdjustmentLedger:
    ledger = session.ledger()
    if request.expected_sha256 != digest(canonical(ledger)):
        raise DataError("ledger: another tab changed the ledger; reload before saving")
    if not request.changes:
        raise DataError("adjustments.changes: at least one adjustment is required")
    players = {p.player.id: p.player for p in session.player_projection(now).players}
    group = str(uuid4())
    entries: list[AdjustmentEntry] = []
    for change in request.changes:
        if change.player_id not in players:
            raise DataError(f"adjustments.{change.player_id}: unknown player")
        entries.append(
            AdjustmentEntry(
                id=str(uuid4()),
                group_id=group,
                player_id=change.player_id,
                team_id=players[change.player_id].team_id,
                field=change.field,
                value=change.value,
                starts_on=change.starts_on,
                ends_on=change.ends_on,
                reason=change.reason,
                created_at=now,
                replaces=change.replaces,
                revokes=(),
            )
        )
    candidate = AdjustmentLedger(format_version=1, entries=(*ledger.entries, *entries))
    validate_ledger(candidate, session.params)
    return candidate


def save_adjustments(session: InseasonSession, request: AdjustmentsRequest) -> JsonValue:
    with session.lock:
        now = datetime.now(UTC)
        candidate = adjustment_entries(session, request, now)
        projection = session.player_projection(now, candidate)
        result: dict[str, JsonValue] = {
            "projection": json_value(projection.model_dump(mode="json")),
            "preview": request.preview,
        }
        if (
            session.preferences.selected_league is not None
            and session.feature_availability(now).enabled
        ):
            result.update(adjustment_impact(session, candidate, now))
        if not request.preview:
            store = session.working_store()
            store.append_snapshot(
                "ledger-history",
                "adjustment ledger",
                now,
                json_value(candidate.model_dump(mode="json")),
            )
            store.write("ledger.json", candidate)
        result["ledger_sha256"] = digest(canonical(candidate))
        return result


def adjustment_impact(
    session: InseasonSession, candidate: AdjustmentLedger, now: datetime
) -> dict[str, JsonValue]:
    sim = session.simulation(ledger=candidate, as_of=now)
    active = next(
        (w for w in sim.league.matchups if w.start <= now.astimezone(sim.zone).date() <= w.end),
        None,
    )
    if active is None:
        return {}
    pair = next(
        p
        for p in sim.snapshot.pairings
        if p.week_id == active.id and sim.snapshot.mine in (p.home, p.away)
    )
    opponent = pair.away if pair.home == sim.snapshot.mine else pair.home
    before = session.simulation(as_of=now).week(sim.snapshot.mine, opponent, active.id)
    after = sim.week(sim.snapshot.mine, opponent, active.id)
    return {
        "before": json_value(before.model_dump(mode="json")),
        "after": json_value(after.model_dump(mode="json")),
    }


def revoke_group(session: InseasonSession, request: RevokeRequest) -> None:
    with session.lock:
        ledger = session.ledger()
        if digest(canonical(ledger)) != request.expected_sha256:
            raise DataError("ledger: changed in another tab; reload")
        group = tuple(e for e in ledger.entries if e.group_id == request.group_id and not e.revokes)
        if not group:
            raise DataError("ledger.group: no adjustment group to revoke")
        now = datetime.now(UTC)
        entry = group[0].model_copy(
            update={
                "id": str(uuid4()),
                "group_id": str(uuid4()),
                "created_at": now,
                "reason": "使用者撤銷整組調整",
                "replaces": None,
                "revokes": tuple(e.id for e in group),
            }
        )
        updated = AdjustmentLedger(format_version=1, entries=(*ledger.entries, entry))
        validate_ledger(updated, session.params)
        session.working_store().append_snapshot(
            "ledger-history", "adjustment ledger", now, json_value(updated.model_dump(mode="json"))
        )
        session.working_store().write("ledger.json", updated)


def redistribution(session: InseasonSession, player: str, minutes: float) -> dict[str, float]:
    return redistribute(session.player_projection(datetime.now(UTC)), player, minutes)


def record_proposal(session: InseasonSession, request: ProposalRequest) -> Proposal:
    store = session.league_store()
    now = datetime.now(UTC)
    if request.supersedes is not None:
        previous = next(
            (
                p
                for p in latest_proposals(
                    tuple(snapshot_record(s, Proposal) for s in store.history("proposals"))
                )
                if p.id == request.supersedes
            ),
            None,
        )
        if previous is None or (previous.opponent, previous.send, previous.receive) != (
            request.opponent,
            request.send,
            request.receive,
        ):
            raise DataError("proposal.supersedes: original proposal does not match")
        proposal = previous.model_copy(
            update={
                "id": str(uuid4()),
                "created_at": now,
                "outcome": request.outcome,
                "supersedes": previous.id,
            }
        )
    else:
        sim = session.simulation(season=True)
        trade = evaluate_trade(
            sim, sim.snapshot.mine, request.opponent, request.send, request.receive
        )
        proposal = Proposal(
            id=str(uuid4()),
            created_at=now,
            opponent=request.opponent,
            send=request.send,
            receive=request.receive,
            rank_delta=trade.rank_delta,
            need_delta=trade.opponent_delta,
            probability=trade.acceptance,
            outcome=request.outcome,
            supersedes=None,
        )
    store.append_snapshot(
        "proposals", "local proposal record", now, json_value(proposal.model_dump(mode="json"))
    )
    return proposal


def refit_acceptance(session: InseasonSession) -> JsonValue:
    proposals = tuple(
        snapshot_record(s, Proposal) for s in session.league_store().history("proposals")
    )
    a, b, threshold, loss, predicted, actual = fit_acceptance(
        proposals, session.params.fit_minimum.value, session.params.tolerance.value
    )
    report: JsonValue = {
        "beta_rank": a,
        "beta_need": b,
        "threshold": threshold,
        "noise": 1.0,
        "log_loss": loss,
        "count": len(predicted),
        "bins": json_value(
            [
                b.model_dump(mode="json")
                for b in calibration_bins(predicted, actual, session.params.calibration_bins.value)
            ]
        ),
    }
    session.league_store().append_snapshot(
        "acceptance-fits", "proposal logistic fit", datetime.now(UTC), report
    )
    return report


def complete_reviews(session: InseasonSession) -> None:
    sim = session.simulation()
    predictions = tuple(
        snapshot_record(s, PredictionRecord) for s in session.league_store().history("predictions")
    )
    recorded = {
        review.week_id: canonical(review)
        for review in latest_reviews(
            tuple(
                snapshot_record(s, WeeklyReview) for s in session.league_store().history("reviews")
            )
        )
    }
    for week in sim.league.matchups:
        if week.end >= sim.as_of.astimezone(sim.zone).date():
            continue
        if not any(p.week_id == week.id for p in predictions):
            continue
        review = weekly_review(
            sim.league, sim.params, sim.snapshot, predictions, week.id, session.notes().adopted
        )
        if recorded.get(week.id) == canonical(review):
            continue
        session.league_store().append_snapshot(
            "reviews",
            "weekly review",
            datetime.now(UTC),
            json_value(review.model_dump(mode="json")),
        )


def import_validation(session: InseasonSession, path: Path) -> None:
    report = decode(BacktestReport, path.read_bytes(), str(path))
    if report.parameters_sha256 != digest(canonical(report.fitted_parameters)):
        raise DataError("validation: fitted parameter hash differs from report")
    if not report.passed:
        raise DataError("validation: required holdout gate failed")
    state = session.state()
    if state.league is None:
        raise DataError("validation: confirm the current league before applying parameters")
    validate_inseason(state.league, report.fitted_parameters)
    session.store.write("parameters.json", report.fitted_parameters)
    session.params = report.fitted_parameters
    session.league_store().write("validation.json", report)


def recorded_plan(session: InseasonSession, plan_id: str) -> AddPlan:
    saved = [
        (snapshot_record(s, PredictionRecord), p)
        for s in session.league_store().history("predictions")
        for p in snapshot_record(s, PredictionRecord).recommendations
        if p.id == plan_id
    ]
    if not saved:
        raise DataError("today.plan_id: unknown recorded F3 plan")
    record, plan = saved[-1]
    state = session.state()
    current_inputs = tuple(
        s
        for s in (state.normalized_sha256, state.players_sha256, state.priors_sha256)
        if s is not None
    )
    if (
        record.input_hashes != current_inputs
        or record.ledger_sha256 != digest(canonical(session.ledger()))
        or record.parameter_sha256 != digest(canonical(session.params))
    ):
        raise DataError("today.plan_id: 資料、手調或參數已更新，請重新計算 F3 計畫")
    return plan
