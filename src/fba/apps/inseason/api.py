import json
from concurrent.futures import CancelledError
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock, Thread
from time import monotonic

from pydantic import JsonValue

from fba.apps.inseason.trade_workers import TradeWorkers
from fba.contracts.base import ConfigError, DataError, Natural, Record
from fba.contracts.inseason import CalculationTimeout, InseasonPreferences
from fba.contracts.inseason_app import (
    AdjustmentsRequest,
    CodeRequest,
    ConfirmSettingsRequest,
    ConnectRequest,
    IgnoreFlag,
    MappingRequest,
    ProposalRequest,
    RevokeRequest,
    SelectLeagueRequest,
    Sources,
    TaskCompletion,
    TodayRequest,
    TradeRequest,
    TradeSearchRequest,
    WeekRequest,
)
from fba.contracts.inseason_results import PredictionRecord, TradeSearchResult, WeeklyReview
from fba.data.codec import canonical, decode, digest
from fba.inseason.operations import (
    bootstrap,
    complete_reviews,
    import_validation,
    latest_plan,
    recommendations,
    record_proposal,
    recorded_plan,
    redistribution,
    refit_acceptance,
    revoke_group,
    save_adjustments,
    week_result,
)
from fba.inseason.review import calibration_history
from fba.inseason.session import InseasonSession, json_value, snapshot_record
from fba.inseason.today import today
from fba.inseason.trades import complementary_teams, evaluate_trade, rank_trades, search_trades


class RedistributionRequest(Record):
    player: str
    minutes: float


class FileRequest(Record):
    path: str


class CancelJobRequest(Record):
    job: Natural


def trade_search(session: InseasonSession, data: bytes, workers: TradeWorkers | None) -> JsonValue:
    request = decode(TradeSearchRequest, data, "trade-search")

    def progress(value: float) -> None:
        session.progress = value

    sim = session.simulation(season=True)
    args = (sim, session.preferences, request.opponent, request.size, progress)
    status = "completed"
    try:
        rows = workers.search(*args, session.cancelled) if workers else search_trades(*args)
    except (CancelledError, CalculationTimeout):
        if not session.cancelled.is_set() or workers is None:
            raise
        rows = rank_trades(sim, workers.completed_trades)
        status = "cancelled"
    result = TradeSearchResult(
        trades=rows,
        counts=sim.trade_search_counts,
        minimum_value_ratio=session.preferences.trade_value_min_ratio,
        status="cancelled" if status == "cancelled" else "completed",
        completed=workers.progress.completed if workers else sim.trade_search_counts["eligible"],
    )
    payload = json_value(result.model_dump(mode="json"))
    session.league_store().append_snapshot(
        "trade-searches", "trade search", datetime.now(UTC), payload
    )
    session.league_store().append_snapshot(
        "trade-search-audits",
        "full simulation and sound bound counts",
        sim.as_of,
        json_value({"status": result.status, "completed": result.completed, **result.counts}),
    )
    return payload


def basic_action(
    session: InseasonSession, action: str, data: bytes, trade_workers: TradeWorkers | None = None
) -> JsonValue:
    if action == "connect":
        request = decode(ConnectRequest, data, action)
        return {
            "url": session.auth.configure(
                request.client_id.get_secret_value(), request.client_secret.get_secret_value()
            )
        }
    if action == "authorize":
        session.auth.exchange(
            decode(CodeRequest, data, action).code.get_secret_value(), datetime.now(UTC)
        )
        return json_value([d.model_dump(mode="json") for d in session.discover()])
    if action == "discover":
        return json_value([d.model_dump(mode="json") for d in session.discover()])
    if action == "select":
        session.select(decode(SelectLeagueRequest, data, action).key)
    elif action == "settings":
        session.confirm_settings(decode(ConfirmSettingsRequest, data, action))
    elif action == "mapping":
        request = decode(MappingRequest, data, action)
        session.map_player(request.external_id, request.player_id)
    elif action == "sources":
        session.configure_sources(decode(Sources, data, action))
    elif action == "preferences":
        preferences = decode(InseasonPreferences, data, action)
        if preferences.selected_league != session.preferences.selected_league:
            raise DataError(
                "preferences.selected_league: select a verified league through the league picker"
            )
        if preferences.stale_warning_seconds > preferences.stale_limit_seconds:
            raise ConfigError("preferences.stale_warning_seconds: exceeds stale limit")
        session.store.write("preferences.json", preferences)
        session.preferences = preferences
    elif action == "adjustments":
        return save_adjustments(session, decode(AdjustmentsRequest, data, action))
    elif action == "revoke":
        revoke_group(session, decode(RevokeRequest, data, action))
    elif action == "redistribute":
        request = decode(RedistributionRequest, data, action)
        return json_value(redistribution(session, request.player, request.minutes))
    else:
        return calculation_action(session, action, data, trade_workers)
    return {"saved": True}


def calculation_action(
    session: InseasonSession, action: str, data: bytes, trade_workers: TradeWorkers | None = None
) -> JsonValue:
    if action == "sync":
        session.synchronize()
        if session.state().players_sha256 is not None:
            complete_reviews(session)
        return {"synced": True}
    if action == "week":
        return json_value(
            week_result(session, decode(WeekRequest, data, action).week_id).model_dump(mode="json")
        )
    if action == "recommendations":
        return json_value(
            [
                p.model_dump(mode="json")
                for p in recommendations(session, decode(WeekRequest, data, action).week_id)
            ]
        )
    if action == "trade":
        request = decode(TradeRequest, data, action)
        sim = session.simulation(season=True)
        with sim.budget(
            "trade_one" if max(len(request.send), len(request.receive)) == 1 else "trade_many"
        ):
            result = evaluate_trade(
                sim, sim.snapshot.mine, request.opponent, request.send, request.receive
            )
        session.league_store().append_snapshot(
            "trade-predictions",
            "trade evaluation",
            sim.as_of,
            json_value(result.model_dump(mode="json")),
        )
        return json_value(result.model_dump(mode="json"))
    if action == "trade-search":
        return trade_search(session, data, trade_workers)
    if action == "partners":
        return json_value(
            [
                row.model_dump(mode="json")
                for row in complementary_teams(session.simulation(season=True))
            ]
        )
    if action == "today":
        return today_result(session, decode(TodayRequest, data, action))

    if action == "proposal":
        return json_value(
            record_proposal(session, decode(ProposalRequest, data, action)).model_dump(mode="json")
        )
    if action == "calibration-history":
        sim = session.simulation()
        store = session.league_store()
        predictions, reviews = store.history("predictions"), store.history("reviews")
        return json_value(
            calibration_history(
                sim.league.season_id,
                tuple(snapshot_record(row, PredictionRecord) for row in predictions),
                tuple(snapshot_record(row, WeeklyReview) for row in reviews),
                tuple(row.sha256 for row in (*predictions, *reviews)),
            ).model_dump(mode="json")
        )
    if action == "refit-acceptance":
        return refit_acceptance(session)
    return note_action(session, action, data)


def today_result(session: InseasonSession, request: TodayRequest) -> JsonValue:
    started = monotonic()
    sim = session.simulation()
    with sim.budget("today"):
        sim.deadline = (started + sim.params.budgets["today"].value, "today")
        sim.check_limits()
        plan = None
        if request.plan_id is not None:
            plan = recorded_plan(session, request.plan_id, sim.check_limits)
        else:
            week = next((w for w in sim.league.matchups if w.start <= request.on <= w.end), None)
            if week is not None:
                plan = latest_plan(session, week.id, sim.check_limits)
        completed = tuple(k for k, v in session.notes().completed.items() if v)
        if plan is not None and any(m.drop in session.preferences.untouchable for m in plan.moves):
            raise DataError("today.plan_id: 計畫包含目前保護的球員，請重新計算 F3")
        team = next(t for t in sim.snapshot.teams if t.id == sim.snapshot.mine)
        if plan is not None and (
            team.adds_used is None
            or len(plan.moves)
            > max(0, sim.league.adds_per_week - team.adds_used - session.preferences.reserve_adds)
        ):
            raise DataError("today.plan_id: 計畫超過目前可用加人額度，請重新計算 F3")
        result = today(sim, request.on, session.preferences.timezone, plan, completed)
        payload = json_value(result.model_dump(mode="json"))
        sim.check_limits()
        session.league_store().append_snapshot(
            "daily-predictions", "daily lineup and actions", sim.as_of, payload
        )
    return payload


def note_action(session: InseasonSession, action: str, data: bytes) -> JsonValue:
    notes = session.notes()
    if action == "complete":
        request = decode(TaskCompletion, data, action)
        notes = notes.model_copy(
            update={"completed": {**notes.completed, request.id: request.completed}}
        )
    elif action == "adopt":
        request = decode(TaskCompletion, data, action)
        notes = notes.model_copy(
            update={"adopted": {**notes.adopted, request.id: request.completed}}
        )
    elif action == "ignore-flag":
        request = decode(IgnoreFlag, data, action)
        notes = notes.model_copy(update={"ignored": {**notes.ignored, request.id: request.until}})
    elif action == "validation":
        import_validation(session, Path(decode(FileRequest, data, action).path))
        return {"saved": True}
    else:
        raise DataError(f"route.{action}: unknown operation")
    session.working_store().write("notes.json", notes)
    return {"saved": True}


class Jobs:
    """One application job at a time; progress reads never wait on calculation."""

    def __init__(self, session: InseasonSession) -> None:
        self.session = session
        self.lock = Lock()
        self.thread: Thread | None = None
        self.result: JsonValue = None
        self.error: str | None = None
        self.identifier = 0
        self.status = "idle"
        self.action = ""
        self.accepting = True
        self.trade_workers = TradeWorkers()

    def start(self, action: str, data: bytes) -> int:
        with self.lock:
            if not self.accepting:
                raise DataError("job: 助手正在結束，已停止接受新工作")
            if self.thread is not None and self.thread.is_alive():
                raise DataError("job: 目前仍在執行，請等待完成")
            self.identifier += 1
            self.action = action
            self.status, self.error, self.result = "running", None, None
            self.session.cancelled.clear()
            if action == "trade-search":
                self.trade_workers.reset_progress()
            self.session.progress = 0.0
            self.session.phase = action
            self.thread = Thread(target=self.run, args=(action, data), daemon=True)
            self.thread.start()
            return self.identifier

    def cancel(self, identifier: int) -> None:
        with self.lock:
            if identifier != self.identifier:
                raise DataError("job: 工作已變更，不能取消其他工作")
            if self.action != "trade-search" or self.status not in {"running", "cancelling"}:
                raise DataError("job: 這項工作目前無法取消")
            self.status = "cancelling"
            self.session.cancelled.set()

    def run(self, action: str, data: bytes) -> None:
        started, at = monotonic(), datetime.now(UTC)
        inputs: list[JsonValue] = []
        parameters_hash = digest(canonical(self.session.params))
        parameter_version = self.session.params.version
        status = "failed"
        try:
            if self.session.preferences.selected_league is not None:
                state = self.session.state()
                inputs = [state.normalized_sha256, state.players_sha256, state.priors_sha256]
            else:
                workspace = self.session.workspace()
                if workspace is not None:
                    inputs = [workspace.players_sha256, workspace.priors_sha256]
            inputs.append(digest(canonical(self.session.ledger())))
            self.result = basic_action(self.session, action, data, self.trade_workers)
            cancelled = isinstance(self.result, dict) and self.result.get("status") == "cancelled"
            status = "cancelled" if action == "trade-search" and cancelled else "completed"
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.result = None
        try:
            self.session.store.append_snapshot(
                "operation-log",
                "local operation",
                at,
                {
                    "operation": action,
                    "parameter_version": parameter_version,
                    "parameter_sha256": parameters_hash,
                    "elapsed_seconds": monotonic() - started,
                    "result": status,
                    "inputs": inputs,
                    "error_type": self.error.split(":", 1)[0] if self.error else None,
                },
            )
        except Exception as exc:
            # A successful action with a failed audit write is visibly incomplete.
            self.error = f"operation-log: {type(exc).__name__}: {exc}"
            status = "failed"
            self.result = None
        with self.lock:
            self.status = status
            self.session.last_error = self.error
            self.session.phase = "idle" if status == "completed" else status
            self.session.progress = 1.0 if status == "completed" else self.session.progress

    def read(self) -> JsonValue:
        return {
            "id": self.identifier,
            "status": self.status,
            "progress": self.session.progress,
            "result": self.result,
            "error": self.error,
            "phase": self.session.phase,
            "action": self.action,
            "can_cancel": self.action == "trade-search" and self.status == "running",
            "search": self.trade_workers.progress.model_dump(mode="json")
            if self.action == "trade-search"
            else None,
        }

    def bootstrap(self) -> JsonValue:
        return bootstrap(self.session)

    def stop(self, seconds: float) -> None:
        self.accepting = False
        self.session.cancelled.set()
        if self.thread is not None:
            self.thread.join(timeout=seconds)
        self.trade_workers.close()


def parse_body(data: bytes) -> tuple[str, bytes]:
    class Envelope(Record):
        action: str
        payload: dict[str, JsonValue]

    request = decode(Envelope, data, "request")
    return request.action, json.dumps(request.payload).encode()
