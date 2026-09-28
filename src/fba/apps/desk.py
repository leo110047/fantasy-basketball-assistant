from collections.abc import Callable
from pathlib import Path
from threading import Condition, Lock, Thread
from time import perf_counter_ns
from typing import Literal

from fba.adapters.codec import canonical, decode, digest, read_bytes
from fba.adapters.desk import log_execution, save_draft
from fba.apps.auction import AuctionSession
from fba.contracts.auction import AuctionInput, AuctionResult, DraftState, MarketUpdate
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
)
from fba.core.auction import (
    ComparisonRunner,
    calculate_auction,
    compare,
    comparison_plans,
    market_context,
    portfolio_for,
)
from fba.core.roster import effective_players

Calculator = Callable[[DraftState, str], AuctionResult]


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
        self.pending: tuple[DraftState, str, int] | None = None
        self.generation = 0
        self.identity = ""
        self.view = JobView(status="updating", result=None, error=None)
        self.stopping = False
        self.thread = Thread(target=self.run, name=f"desk-{mode}")
        self.thread.start()

    def request(self, state: DraftState, sha: str) -> None:
        with self.condition:
            self.generation += 1
            self.identity = sha
            self.pending = (state, sha, self.generation)
            self.view = JobView(status="updating", result=None, error=None)
            self.condition.notify()

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
                state, sha, generation = self.pending
                self.pending = None
            started = perf_counter_ns()
            try:
                result = self.calculate(state, sha)
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
                view = JobView(status="ready", result=result, error=None)
            except Exception as exc:
                # This is the process boundary: report the failure, keep the saved ledger.
                view = self.failure(state, sha, started, exc)
            with self.condition:
                if generation == self.generation:
                    self.view = view

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
        self.desk = self.market(state)
        self.equal = LatestCalculation(equal, self.record, "equal")
        self.fit = LatestCalculation(fit, self.record, "fit")
        self.request()

    def record(self, record: DeskExecution) -> None:
        with self.log_lock:
            log_execution(self.log, record)

    def market(self, state: DraftState) -> DeskState:
        start = perf_counter_ns()
        sha = digest(canonical(state))
        update = MarketUpdate(
            format_version=1,
            config=self.inputs.config.refs,
            input_sha256=self.input_hash,
            state_sha256=sha,
            market=market_context(self.inputs, state, self.input_hash)[1],
        )
        self.record(
            DeskExecution(
                format_version=1,
                stage="market",
                state=state,
                state_sha256=sha,
                elapsed_ns=perf_counter_ns() - start,
                solver_calls=0,
                result=update,
            )
        )
        return DeskState(format_version=1, state=state, market=update)

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
            candidate = request.draft.model_copy(update={"revision": self.desk.state.revision + 1})
            updated = self.market(candidate)
            payload = save_draft(self.path, candidate, self.payload)
            self.payload = payload
            self.desk = updated
            return self.desk

    def start_calculations(self, sha: str) -> None:
        with self.lock:
            if self.desk.market.state_sha256 == sha:
                self.request()

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
            if self.equal.current(request.state_sha256).status != "ready":
                raise DataError("comparison: current additive calculation is not ready")
        start = perf_counter_ns()
        players = effective_players(self.inputs.config.league, self.inputs.players, state)
        portfolio = portfolio_for(self.inputs, players, market, state)
        result = Compared(
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
            job.close()


def session_calculator(
    inputs: AuctionInput, sha: str, session: AuctionSession, mode: Literal["equal", "fit"]
) -> Calculator:
    def calculate(state: DraftState, state_hash: str) -> AuctionResult:
        return calculate_auction(
            inputs,
            state,
            sha,
            state_hash,
            runner=session.caps,
            mode=mode,
            kernel=session.native() if mode == "fit" else None,
            feature_runner=session.features if mode == "fit" else None,
        )

    return calculate
