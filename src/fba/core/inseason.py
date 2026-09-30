from datetime import datetime

from fba.contracts.base import ConfigError, DataError
from fba.contracts.config import Linear
from fba.contracts.inseason import (
    AdjustmentEntry,
    AdjustmentLedger,
    FeatureAvailability,
    InseasonLeague,
    InseasonParameters,
    InseasonPreferences,
    ProjectionRules,
    SyncState,
)
from fba.core.config import require_members, unique, validate_periods


def validate_inseason(league: InseasonLeague, params: InseasonParameters) -> None:
    unique(league.base_stats, "league.base_stats")
    unique(tuple(c.id for c in league.categories), "league.categories")
    unique(tuple(d.id for d in league.derived), "league.derived")
    unique(tuple(s.id for s in league.starter_slots), "league.starter_slots")
    unique(tuple(f.id for f in params.fields), "parameters.fields")
    available = set(league.base_stats)
    for derived in league.derived:
        require_members(tuple(t.stat_id for t in derived.terms), available, f"derived.{derived.id}")
        if derived.id in available:
            raise ConfigError(f"derived.{derived.id}: duplicate base or derived statistic")
        available.add(derived.id)
    for c in league.categories:
        terms = (
            c.formula.terms
            if isinstance(c.formula, Linear)
            else c.formula.numerator + c.formula.denominator
        )
        require_members(tuple(t.stat_id for t in terms), available, f"categories.{c.id}")
    for s in league.starter_slots:
        require_members(s.eligible_positions, set(league.positions), f"starter_slots.{s.id}")
    required = required_statistics(league)
    require_members(required, set(params.rate_k), "parameters.rate_k")
    for shot in league.shots:
        require_members((shot.made, shot.attempted), set(league.base_stats), f"shots.{shot.id}")
    require_members(
        tuple(s.id for s in league.shots if s.made in required),
        set(params.shot_k),
        "parameters.shot_k",
    )
    validate_parameter_values(params)
    validate_periods(league.matchups, league.starts_on, league.ends_on, "league.matchups")
    require_members(league.playoff_weeks, {w.id for w in league.matchups}, "league.playoff_weeks")
    if league.playoff_teams > league.teams:
        raise ConfigError("league.playoff_teams: exceeds team count")
    targets = {
        "minutes",
        "q",
        *("rate:" + s for s in league.base_stats),
        *("shot:" + s.id for s in league.shots),
    }
    for field in params.fields:
        require_members(field.targets, targets, f"parameters.fields.{field.id}.targets")
        if field.minimum > field.maximum or (field.kind == "status" and not field.statuses):
            raise ConfigError(f"parameters.fields.{field.id}: invalid bounds or status mapping")


def validate_parameter_values(params: InseasonParameters) -> None:
    positive = ("minute_k", "minute_half_life", "acceptance_noise", "request_timeout", "tolerance")
    for key in positive:
        if getattr(params, key).value <= 0:
            raise ConfigError(f"parameters.{key}.value: must be positive")
    for key, parameter in (*params.rate_k.items(), *params.shot_k.items()):
        if parameter.value <= 0:
            raise ConfigError(f"parameters.k.{key}.value: must be positive")
    for key in (
        "simulations",
        "season_simulations",
        "shortlist",
        "beam_width",
        "max_trade_players",
        "calibration_bins",
        "role_window",
        "override_window",
        "maximum_requests",
        "fit_minimum",
    ):
        if getattr(params, key).value <= 0:
            raise ConfigError(f"parameters.{key}.value: must be positive")
    validate_operational_parameters(params)
    probabilities = {
        "calibration": params.calibration,
        "safe_probability": params.safe_probability,
        "abandon_probability": params.abandon_probability,
        **params.availability,
    }
    for key, parameter in probabilities.items():
        if not 0 <= parameter.value <= 1:
            raise ConfigError(f"parameters.{key}.value: must be a probability")
    if params.abandon_probability.value >= params.safe_probability.value:
        raise ConfigError("parameters: abandon_probability must be below safe_probability")


def validate_operational_parameters(params: InseasonParameters) -> None:
    for key in (
        "retry_seconds",
        "role_threshold",
        "override_threshold",
        "production_sigma",
        "calibration_alert",
        "rank_scale",
        "rank_exponent",
    ):
        if getattr(params, key).value < 0:
            raise ConfigError(f"parameters.{key}.value: must be nonnegative")
    required_budgets = (
        "sync",
        "projection",
        "team",
        "week",
        "recommendations",
        "trade_one",
        "trade_many",
        "today",
    )
    require_members(required_budgets, set(params.budgets), "parameters.budgets")
    for key, parameter in params.budgets.items():
        if parameter.value <= 0:
            raise ConfigError(f"parameters.budgets.{key}.value: must be positive")
    for group in params.prior_groups:
        if group.minimum_minutes > group.maximum_minutes or not group.positions:
            raise ConfigError(
                f"parameters.prior_groups.{group.id}: invalid range or empty positions"
            )


def availability(
    state: SyncState,
    preferences: InseasonPreferences,
    now: datetime,
    *,
    players_as_of: datetime | None,
) -> FeatureAvailability:
    reason, repair = None, None
    if not state.connected:
        reason, repair = "尚未連結 Yahoo", "到資料與同步頁連結自己的 Yahoo API 帳號"
    elif not state.authorization_valid:
        reason, repair = "Yahoo 授權失效，需要重新連結", "重新授權後執行同步"
    elif state.last_success is None:
        reason, repair = "尚無成功同步的聯盟資料", "完成第一次同步"
    elif (now - state.last_success).total_seconds() > preferences.stale_limit_seconds:
        reason, repair = "聯盟資料已超過時效上限", "恢復連線並重新同步"
    elif players_as_of is None:
        reason, repair = "尚無 NBA 球員資料", "設定球員資料來源並完成同步"
    elif (now - players_as_of).total_seconds() > preferences.stale_limit_seconds:
        reason, repair = "NBA 球員資料已超過時效上限", "恢復 NBA 資料來源並重新同步"
    elif state.settings_pending:
        reason, repair = "聯盟設定有差異", "確認匯入 Yahoo 設定或保留本機設定"
    elif state.unresolved_rostered:
        reason, repair = "隊伍名單有未對照球員", "完成資料與同步頁的球員對照"
    return FeatureAvailability(
        enabled=reason is None, reason=reason, repair=repair, as_of=state.last_success
    )


def require_available(
    state: SyncState,
    preferences: InseasonPreferences,
    now: datetime,
    *,
    players_as_of: datetime | None,
) -> None:
    status = availability(state, preferences, now, players_as_of=players_as_of)
    if not status.enabled:
        raise DataError(f"league: {status.reason}；{status.repair}")


def validate_ledger(ledger: AdjustmentLedger, params: InseasonParameters) -> None:
    unique(tuple(e.id for e in ledger.entries), "ledger.entries.id")
    fields = {f.id: f for f in params.fields}
    previous: dict[str, AdjustmentEntry] = {}
    for entry in ledger.entries:
        if entry.starts_on > entry.ends_on or not entry.reason.strip():
            raise DataError(f"ledger.{entry.id}: reversed dates or empty reason")
        if previous and entry.created_at < max(e.created_at for e in previous.values()):
            raise DataError(f"ledger.{entry.id}: creation times must be ordered")
        if entry.revokes:
            if any(i not in previous or previous[i].revokes for i in entry.revokes):
                raise DataError(f"ledger.{entry.id}.revokes: unknown entry or revocation")
        elif entry.field not in fields:
            raise DataError(f"ledger.{entry.id}.field: unknown field")
        else:
            field = fields[entry.field]
            if field.kind == "status":
                if not isinstance(entry.value, str) or entry.value not in field.statuses:
                    raise DataError(f"ledger.{entry.id}.value: unknown status")
            elif isinstance(entry.value, str) or not field.minimum <= entry.value <= field.maximum:
                raise DataError(f"ledger.{entry.id}.value: outside configured bounds")
        if entry.replaces is not None:
            old = previous.get(entry.replaces)
            if old is None or (old.player_id, old.field) != (entry.player_id, entry.field):
                raise DataError(f"ledger.{entry.id}.replaces: mismatched adjustment")
        previous[entry.id] = entry


def required_statistics(rules: ProjectionRules) -> tuple[str, ...]:
    available = set(rules.base_stats)
    for definition in rules.derived:
        require_members(
            tuple(t.stat_id for t in definition.terms), available, f"derived.{definition.id}"
        )
        if definition.id in available:
            raise ConfigError(f"derived.{definition.id}: duplicate statistic")
        available.add(definition.id)
    pending = {
        t.stat_id
        for c in rules.categories
        for t in (
            c.formula.terms
            if isinstance(c.formula, Linear)
            else (*c.formula.numerator, *c.formula.denominator)
        )
    }
    derived = {d.id: d for d in rules.derived}
    required: set[str] = set()
    while pending:
        stat = pending.pop()
        if stat in derived:
            pending.update(t.stat_id for t in derived[stat].terms)
        else:
            required.add(stat)
    for shot in rules.shots:
        if shot.made in required:
            required.add(shot.attempted)
    require_members(tuple(required), set(rules.base_stats), "projection.base_stats")
    return tuple(s for s in rules.base_stats if s in required)
