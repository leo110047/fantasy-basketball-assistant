from collections.abc import Callable
from functools import partial
from pathlib import Path
from threading import Condition, Event, Lock, Thread
from time import perf_counter_ns
from typing import Literal

from fba.adapters.codec import canonical, decode, digest, read_bytes
from fba.adapters.desk import log_execution, save_draft
from fba.apps.auction import AuctionSession, CancellableKernel
from fba.apps.workers import check_current
from fba.contracts.auction import (
    AuctionInput,
    AuctionResult,
    DraftState,
    ManagedFitSummary,
    MarketUpdate,
)
from fba.contracts.base import DataError
from fba.contracts.desk import (
    Compared,
    CompareRequest,
    DeskBootstrap,
    DeskError,
    DeskExecution,
    DeskResults,
    DeskState,
    JobView,
    SaveDraft,
    SaveUnconfirmed,
)
from fba.core.auction import (
    ComparisonRunner,
    calculate_auction,
    compare,
    comparison_plans,
    market_context,
    portfolio_for,
)
from fba.core.roster import effective_players, validate_labels

Calculator = Callable[[DraftState, str, Event], AuctionResult]


def same_calculation(before: DraftState, after: DraftState) -> bool:
    return (
        before.config == after.config
        and before.input_sha256 == after.input_sha256
        and before.mine == after.mine
        and tuple(t.id for t in before.teams) == tuple(t.id for t in after.teams)
        and before.sales == after.sales
        and before.overrides == after.overrides
    )


class LatestCalculation:
    """One running calculation and one replaceable latest request, with no result cache."""

    def __init__(
        self,
        calculate: Calculator,
        record: Callable[[DeskExecution], None],
        mode: Literal["equal", "fit"],
    ) -> None:
        self.calculate = calculate
        self.record = record
        self.mode: Literal["equal", "fit"] = mode
        self.condition = Condition()
        self.pending: tuple[DraftState, str, int, Event] | None = None
        self.cancelled = Event()
        self.generation = 0
        self.identity = ""
        self.latest: tuple[DraftState, str] | None = None
        self.view = JobView(status="updating", result=None, error=None)
        self.stopping = False
        self.thread = Thread(target=self.run, name=f"desk-{mode}")
        self.thread.start()

    def request(self, state: DraftState, sha: str) -> None:
        with self.condition:
            self.cancelled.set()
            self.cancelled = Event()
            self.generation += 1
            self.identity = sha
            self.latest = (state, sha)
            self.pending = (state, sha, self.generation, self.cancelled)
            self.view = JobView(status="updating", result=None, error=None)
            self.condition.notify()

    def relabel(self, state: DraftState, sha: str) -> bool:
        with self.condition:
            if (
                self.latest is None
                or self.cancelled.is_set()
                or not same_calculation(self.latest[0], state)
            ):
                return False
            self.latest = (state, sha)
            self.identity = sha
            if self.pending is not None:
                self.pending = (state, sha, self.generation, self.cancelled)
            if self.view.result is not None:
                self.view = self.view.model_copy(
                    update={"result": self.view.result.model_copy(update={"state_sha256": sha})}
                )
            return True

    def invalidate(self) -> None:
        with self.condition:
            self.cancelled.set()
            self.pending = None

    def current(self, sha: str) -> JobView:
        with self.condition:
            if sha != self.identity:
                return JobView(status="updating", result=None, error=None)
            return self.view

    def run(self) -> None:
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.stopping or self.pending is not None)
                if self.stopping:
                    return
                assert self.pending is not None
                state, sha, generation, cancelled = self.pending
                self.pending = None
            started = perf_counter_ns()
            try:
                outcome: AuctionResult | Exception = self.calculate(state, sha, cancelled)
            except Exception as exc:
                outcome = exc
            with self.condition:
                if generation != self.generation or cancelled.is_set():
                    continue
                assert self.latest is not None
                state, sha = self.latest
                self.view = self.finish(state, sha, started, outcome)

    def finish(
        self, state: DraftState, sha: str, started: int, outcome: AuctionResult | Exception
    ) -> JobView:
        if isinstance(outcome, Exception):
            return self.failure(state, sha, started, outcome)
        result = outcome.model_copy(update={"state_sha256": sha})
        try:
            self.record(
                DeskExecution(
                    format_version=1,
                    stage=self.mode,
                    state=state,
                    state_sha256=sha,
                    elapsed_ns=perf_counter_ns() - started,
                    solver_calls=result.solver_calls,
                    result=result,
                )
            )
        except Exception as exc:
            return self.failure(state, sha, started, exc)
        return JobView(status="ready", result=result, error=None)

    def failure(self, state: DraftState, sha: str, started: int, exc: Exception) -> JobView:
        error = f"{type(exc).__name__}: {exc}"
        try:
            self.record(
                DeskExecution(
                    format_version=1,
                    stage=self.mode,
                    state=state,
                    state_sha256=sha,
                    elapsed_ns=perf_counter_ns() - started,
                    solver_calls=None,
                    result=DeskError(error=error),
                )
            )
        except Exception as log_error:
            error += f"; failure log unavailable: {type(log_error).__name__}: {log_error}"
        return JobView(status="failed", result=None, error=error)

    def close(self) -> None:
        with self.condition:
            self.stopping = True
            self.cancelled.set()
            self.condition.notify()
        self.thread.join()


class AuctionDesk:
    def __init__(
        self,
        inputs: AuctionInput,
        input_hash: str,
        draft: Path,
        log: Path,
        equal: Calculator,
        fit: Calculator,
        comparison_runner: ComparisonRunner = comparison_plans,
    ) -> None:
        self.inputs = inputs
        self.input_hash = input_hash
        self.comparison_runner = comparison_runner
        self.path = draft
        self.log = log
        self.lock = Lock()
        self.log_lock = Lock()
        self.payload = read_bytes(draft)
        state = decode(DraftState, self.payload, str(draft))
        self.desk, entry = self.market(state)
        self.record(entry)
        self.equal = LatestCalculation(equal, self.record, "equal")
        self.fit = LatestCalculation(fit, self.record, "fit")
        self.request()

    def record(self, record: DeskExecution) -> None:
        with self.log_lock:
            log_execution(self.log, record)

    def market(self, state: DraftState) -> tuple[DeskState, DeskExecution]:
        start = perf_counter_ns()
        sha = digest(canonical(state))
        update = MarketUpdate(
            format_version=1,
            config=self.inputs.config.refs,
            input_sha256=self.input_hash,
            state_sha256=sha,
            market=market_context(self.inputs, state, self.input_hash)[1],
        )
        entry = DeskExecution(
            format_version=1,
            stage="market",
            state=state,
            state_sha256=sha,
            elapsed_ns=perf_counter_ns() - start,
            solver_calls=0,
            result=update,
        )
        return DeskState(format_version=1, state=state, market=update), entry

    def request(self) -> None:
        for job in (self.equal, self.fit):
            job.request(self.desk.state, self.desk.market.state_sha256)

    def bootstrap(self) -> DeskBootstrap:
        with self.lock:
            return DeskBootstrap(
                format_version=1,
                season_id=self.inputs.config.season.season_id,
                snapshot_sha256=self.inputs.snapshot_sha256,
                league=self.inputs.config.league,
                players=effective_players(
                    self.inputs.config.league, self.inputs.players, self.desk.state
                ),
                desk=self.desk,
            )

    def save(self, request: SaveDraft) -> DeskState:
        with self.lock:
            self.require_state(request.expected_sha256)
            if request.draft.draft_id != self.desk.state.draft_id:
                raise DataError("draft: imported backup belongs to a different draft")
            candidate = request.draft.model_copy(update={"revision": self.desk.state.revision + 1})
            validate_labels(self.inputs.players, candidate)
            if same_calculation(self.desk.state, candidate):
                sha = digest(canonical(candidate))
                updated = DeskState(
                    format_version=1,
                    state=candidate,
                    market=self.desk.market.model_copy(update={"state_sha256": sha}),
                )
                entry = None
            else:
                updated, entry = self.market(candidate)
            try:
                payload = save_draft(self.path, candidate, self.payload)
            except SaveUnconfirmed:
                self.accept_saved(updated, canonical(candidate))
                raise
            self.accept_saved(updated, payload)
            try:
                if entry is not None:
                    self.record(entry)
            except DataError as exc:
                raise SaveUnconfirmed(f"draft saved; execution log failed: {exc}") from exc
            return self.desk

    def accept_saved(self, updated: DeskState, payload: bytes) -> None:
        relabel = same_calculation(self.desk.state, updated.state)
        self.payload = payload
        self.desk = updated
        for job in (self.equal, self.fit):
            if not relabel or not job.relabel(updated.state, updated.market.state_sha256):
                job.invalidate()

    def start_calculations(self, sha: str) -> None:
        with self.lock:
            if self.desk.market.state_sha256 == sha:
                for job in (self.equal, self.fit):
                    if job.identity != sha:
                        job.request(self.desk.state, sha)

    def require_state(self, sha: str) -> None:
        if self.desk.market.state_sha256 != sha:
            raise DataError("draft: another change was saved; reload before retrying")

    def results(self) -> DeskResults:
        with self.lock:
            sha = self.desk.market.state_sha256
            return DeskResults(
                state_sha256=sha, equal=self.equal.current(sha), fit=self.fit.current(sha)
            )

    def retry(self, sha: str) -> DeskResults:
        with self.lock:
            self.require_state(sha)
            self.request()
        return self.results()

    def comparison(self, request: CompareRequest) -> Compared:
        with self.lock:
            self.require_state(request.state_sha256)
            state, market = self.desk.state, self.desk.market.market
            if not next(team.slots for team in market.room if team.id == state.mine):
                raise DataError("comparison: own roster is already complete")
            job = (self.equal if request.mode == "equal" else self.fit).current(
                request.state_sha256
            )
            if job.status != "ready" or job.result is None:
                raise DataError(f"comparison: current {request.mode} calculation is not ready")
            fitted = job.result.fit
            if request.mode == "fit" and not isinstance(fitted, ManagedFitSummary):
                raise DataError(
                    "comparison: rebuild with a managed pricing model for fit comparison"
                )
        start = perf_counter_ns()
        players = effective_players(self.inputs.config.league, self.inputs.players, state)
        if request.mode == "fit" and isinstance(fitted, ManagedFitSummary):
            utilities = {p.id: p.utility for p in fitted.players}
            if set(utilities) != {p.id for p in players}:
                raise DataError("comparison: fitted player population differs from current state")
            players = tuple(p.model_copy(update={"utility": utilities[p.id]}) for p in players)
        portfolio = portfolio_for(self.inputs, players, market, state)
        result = Compared(
            mode=request.mode,
            state_sha256=request.state_sha256,
            comparison=compare(portfolio, request.player_id, request.price, self.comparison_runner),
        )
        self.record(
            DeskExecution(
                format_version=1,
                stage="compare",
                state=state,
                state_sha256=request.state_sha256,
                elapsed_ns=perf_counter_ns() - start,
                solver_calls=result.comparison.solver_calls,
                result=result,
            )
        )
        with self.lock:
            self.require_state(request.state_sha256)
        return result

    def close(self) -> None:
        for job in (self.equal, self.fit):
            job.invalidate()
        for job in (self.equal, self.fit):
            job.close()


def session_calculator(
    inputs: AuctionInput, sha: str, session: AuctionSession, mode: Literal["equal", "fit"]
) -> Calculator:
    def calculate(state: DraftState, state_hash: str, cancelled: Event) -> AuctionResult:
        check_current(cancelled)
        result = calculate_auction(
            inputs,
            state,
            sha,
            state_hash,
            runner=partial(session.caps, cancelled=cancelled),
            mode=mode,
            kernel=CancellableKernel(session.native(), cancelled) if mode == "fit" else None,
            feature_runner=partial(session.features, cancelled=cancelled)
            if mode == "fit"
            else None,
        )
        check_current(cancelled)
        return result

    return calculate
