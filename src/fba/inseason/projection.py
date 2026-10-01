import json
from datetime import date, datetime, timedelta
from hashlib import sha256
from math import fsum
from zoneinfo import ZoneInfo

from fba.contracts.base import DataError
from fba.contracts.formula import FormulaTrace
from fba.contracts.inseason import (
    AdjustmentEntry,
    AdjustmentLedger,
    BoxScore,
    EffectivePlayer,
    EffectiveProjection,
    FrozenPriors,
    InseasonParameters,
    PlayerPrior,
    PlayerSnapshot,
    ProjectionFlag,
    ProjectionRules,
    SeasonGame,
    SeasonPlayer,
)
from fba.core.config import require_members
from fba.core.inseason import required_statistics
from fba.formulas.registry import evaluate
from fba.inseason.adjustments import active_entries


class ProjectionIndex:
    """A simulation owns lookups for its immutable effective-player profiles."""

    def __init__(self) -> None:
        self.profiles: dict[
            int, tuple[tuple[EffectivePlayer, ...], dict[str, EffectivePlayer]]
        ] = {}

    def get(self, projection: EffectiveProjection) -> dict[str, EffectivePlayer]:
        key = id(projection.players)
        if key not in self.profiles:
            self.profiles[key] = (
                projection.players,
                {p.player.id: p for p in projection.players},
            )
        return self.profiles[key][1]


def visible_players(snapshot: PlayerSnapshot, as_of: datetime) -> tuple[SeasonPlayer, ...]:
    latest: dict[str, SeasonPlayer] = {}
    for p in sorted(snapshot.players, key=lambda p: (p.known_at, p.id)):
        if p.known_at <= as_of:
            latest[p.id] = p
    return tuple(latest[k] for k in sorted(latest))


def visible_games(snapshot: PlayerSnapshot, as_of: datetime) -> tuple[SeasonGame, ...]:
    latest: dict[str, SeasonGame] = {}
    for game in sorted(snapshot.games, key=lambda g: (g.known_at, g.id)):
        if game.known_at <= as_of:
            latest[game.id] = game
    return tuple(
        sorted(
            (g for g in latest.values() if g.status not in ("cancelled", "postponed")),
            key=lambda g: (g.tipoff, g.id),
        )
    )


def effective_status(player: SeasonPlayer, today: date, on: date) -> str:
    return (
        "healthy"
        if player.return_on is not None and today < player.return_on <= on
        else player.status
    )


def back_to_back_teams(games: tuple[SeasonGame, ...], zone: ZoneInfo, on: date) -> set[str]:
    return {
        team
        for game in games
        if game.tipoff.astimezone(zone).date() == on - timedelta(days=1)
        for team in (game.home, game.away)
    }


def projection_profile_key(
    snapshot: PlayerSnapshot,
    ledger: AdjustmentLedger,
    params: InseasonParameters,
    games: tuple[SeasonGame, ...],
    zone: ZoneInfo,
    as_of: datetime,
    on: date,
) -> tuple[tuple[str, ...], ...]:
    """Date-dependent inputs at a fixed decision time; never cross an evidence update."""
    players = visible_players(snapshot, as_of)
    entries = active_entries(ledger, on, as_of)
    fields = {f.id: f for f in params.fields}
    teams = back_to_back_teams(games, zone, on)
    player_teams = {p.id: p.team_id for p in players}
    return (
        tuple(e.id for e in entries),
        tuple(effective_status(p, as_of.astimezone(zone).date(), on) for p in players),
        tuple(
            e.id
            for e in entries
            if fields[e.field].only_back_to_back and player_teams.get(e.player_id) in teams
        ),
    )


def fallback_prior(
    player: SeasonPlayer,
    boxes: tuple[BoxScore, ...],
    players: tuple[SeasonPlayer, ...],
    priors: FrozenPriors,
    params: InseasonParameters,
) -> PlayerPrior:
    minutes = evaluate("mean", values=tuple(b.minutes for b in boxes)).result if boxes else 0.0
    groups = tuple(
        g
        for g in params.prior_groups
        if set(g.positions).intersection(player.positions)
        and g.minimum_minutes <= minutes < g.maximum_minutes
    )
    if not groups:
        raise DataError(f"prior.{player.id}: no matching position/minutes prior group")
    positions = {p.id: set(p.positions) for p in players}
    # A multi-position player belongs to the union of matching peer groups.
    # Iterate priors once so peers eligible at multiple positions get one vote.
    peers = tuple(
        p
        for p in priors.players
        if p.player_id != player.id
        and any(
            positions.get(p.player_id, set()).intersection(group.positions, player.positions)
            and group.minimum_minutes <= p.minutes < group.maximum_minutes
            for group in groups
        )
    )
    if not peers:
        raise DataError(f"prior.{player.id}: no observed peers in matching groups")
    return PlayerPrior(
        player_id=player.id,
        minutes=evaluate("mean", values=tuple(p.minutes for p in peers)).result,
        rates={
            s: evaluate("mean", values=tuple(p.rates[s] for p in peers)).result
            for s in peers[0].rates
        },
        probabilities={
            s: evaluate(
                "mean", values=tuple(p.probabilities[s] for p in peers if s in p.probabilities)
            ).result
            for s in {key for p in peers for key in p.probabilities}
        },
    )


def observed_boxes(snapshot: PlayerSnapshot, as_of: datetime) -> dict[str, tuple[BoxScore, ...]]:
    latest: dict[tuple[str, str], BoxScore] = {}
    for box in sorted(snapshot.boxes, key=lambda b: (b.known_at, b.played_at, b.game_id)):
        if box.known_at <= as_of and box.played_at < as_of:
            latest[box.player_id, box.game_id] = box
    grouped: dict[str, list[BoxScore]] = {}
    for box in sorted(latest.values(), key=lambda b: (b.played_at, b.game_id)):
        grouped.setdefault(box.player_id, []).append(box)
    return {k: tuple(v) for k, v in grouped.items()}


def blend_player(
    player: SeasonPlayer,
    prior: PlayerPrior,
    boxes: tuple[BoxScore, ...],
    league: ProjectionRules,
    params: InseasonParameters,
) -> tuple[
    float,
    dict[str, float],
    dict[str, float],
    dict[str, FormulaTrace],
    dict[str, float],
    dict[str, float],
]:
    required = required_statistics(league)
    require_members(required, set(params.rate_k), "parameters.rate_k")
    for box in boxes:
        missing = set(required) - box.stats.keys()
        if missing:
            raise DataError(f"box.{box.game_id}.{player.id}: missing statistics {sorted(missing)}")
    total_minutes = fsum(b.minutes for b in boxes)
    totals = {s: fsum(b.stats[s] for b in boxes) for s in required}
    traces: dict[str, FormulaTrace] = {}
    rates: dict[str, float] = {}
    weights: dict[str, float] = {}
    for s in required:
        if s not in prior.rates:
            raise DataError(f"prior.{player.id}.rates.{s}: missing required statistic")
        traces[s] = evaluate(
            "blend",
            k=params.rate_k[s].value,
            prior=prior.rates[s],
            total=totals[s],
            sample=total_minutes,
        )
        rates[s] = traces[s].result
        traces["weight:" + s] = evaluate("weight", sample=total_minutes, k=params.rate_k[s].value)
        weights[s] = traces["weight:" + s].result
        if total_minutes:
            traces["current:" + s] = evaluate(
                "ratio", numerator=totals[s], denominator=total_minutes, zero_value=0.0
            )
    shots: dict[str, float] = {}
    for shot in league.shots:
        if shot.made not in rates:
            continue
        if shot.id not in prior.probabilities:
            raise DataError(f"prior.{player.id}.probabilities.{shot.id}: missing prior probability")
        traces[shot.id] = evaluate(
            "blend",
            k=params.shot_k[shot.id].value,
            prior=prior.probabilities[shot.id],
            total=totals[shot.made],
            sample=totals[shot.attempted],
        )
        shots[shot.id] = traces[shot.id].result
        rates[shot.made] = evaluate(
            "product", gain=rates[shot.attempted], probability=shots[shot.id]
        ).result
        traces["weight:" + shot.id] = evaluate(
            "weight", sample=totals[shot.attempted], k=params.shot_k[shot.id].value
        )
        weights[shot.id] = traces["weight:" + shot.id].result
        if totals[shot.attempted]:
            traces["current:" + shot.id] = evaluate(
                "ratio",
                numerator=totals[shot.made],
                denominator=totals[shot.attempted],
                zero_value=0.0,
            )
    traces["minutes"] = evaluate(
        "minutes",
        k=params.minute_k.value,
        prior=prior.minutes,
        minutes=tuple(b.minutes for b in boxes),
        half_life=params.minute_half_life.value,
    )
    return traces["minutes"].result, rates, shots, traces, weights, totals


def adjusted_values(
    minutes: float,
    q: float,
    rates: dict[str, float],
    shots: dict[str, float],
    entries: tuple[AdjustmentEntry, ...],
    params: InseasonParameters,
    back_to_back: bool,
) -> tuple[float, float, dict[str, float], dict[str, float], dict[str, FormulaTrace]]:
    values = {
        "minutes": minutes,
        "q": q,
        **{"rate:" + k: v for k, v in rates.items()},
        **{"shot:" + k: v for k, v in shots.items()},
    }
    fields = {f.id: f for f in params.fields}
    traces: dict[str, FormulaTrace] = {}
    for entry in sorted(entries, key=lambda e: fields[e.field].only_back_to_back):
        field = fields[entry.field]
        if field.only_back_to_back and not back_to_back:
            continue
        value = field.statuses[str(entry.value)] if field.kind == "status" else float(entry.value)
        for target in field.targets:
            if target not in values:
                continue  # This league does not score the target statistic.
            if field.kind == "multiply":
                trace = evaluate("product", gain=values[target], probability=value)
                traces[f"adjustment:{entry.id}:{target}"] = trace
                values[target] = trace.result
            else:
                values[target] = value
    return (
        values["minutes"],
        values["q"],
        {s: values["rate:" + s] for s in rates},
        {s: values["shot:" + s] for s in shots},
        traces,
    )


def player_flags(
    player: SeasonPlayer,
    boxes: tuple[BoxScore, ...],
    minutes: float,
    rates: dict[str, float],
    entries: tuple[AdjustmentEntry, ...],
    params: InseasonParameters,
) -> tuple[ProjectionFlag, ...]:
    flags: list[ProjectionFlag] = []
    recent = boxes[-params.role_window.value :]
    recent_trace = evaluate("mean", values=tuple(b.minutes for b in recent)) if recent else None
    mean = recent_trace.result if recent_trace else minutes
    role_error = evaluate("absolute_error", predicted=minutes, observed=mean)
    if len(recent) >= params.role_window.value and role_error.result > params.role_threshold.value:
        flags.append(
            ProjectionFlag(
                id=f"{player.id}:role",
                player_id=player.id,
                field="minutes",
                kind="role",
                model=minutes,
                observed=mean,
                reason="近期分鐘與模型差距超過設定門檻",
                traces=(*((recent_trace,) if recent_trace else ()), role_error),
            )
        )
    for stat, rate in rates.items():
        if len(boxes) < max(2, params.role_window.value) or fsum(b.minutes for b in boxes) <= 0:
            continue
        values = {
            "counts": tuple(b.stats[stat] for b in boxes),
            "exposure": tuple(b.minutes for b in boxes),
        }
        average = evaluate("exposure_rate", **values)
        se = evaluate("exposure_error", **values, model=rate)
        threshold = evaluate("product", gain=se.result, probability=params.production_sigma.value)
        production_error = evaluate("absolute_error", predicted=rate, observed=average.result)
        if production_error.result > threshold.result:
            flags.append(
                ProjectionFlag(
                    id=f"{player.id}:production:{stat}",
                    player_id=player.id,
                    field=stat,
                    kind="production",
                    model=rate,
                    observed=average.result,
                    reason="當季每分鐘產出偏離模型超過抽樣誤差門檻",
                    traces=(average, se, threshold, production_error),
                )
            )
    fields = {f.id: f for f in params.fields}
    for entry in entries:
        if entry.team_id != player.team_id:
            flags.append(
                ProjectionFlag(
                    id=f"{player.id}:team:{entry.id}",
                    player_id=player.id,
                    field=entry.field,
                    kind="team_changed",
                    model=minutes,
                    observed=mean,
                    reason="球隊已變更，請確認",
                    traces=(recent_trace,) if recent_trace else (),
                )
            )
        if "minutes" in fields[entry.field].targets and len(boxes) >= params.override_window.value:
            recent_mean = evaluate(
                "mean", values=tuple(b.minutes for b in boxes[-params.override_window.value :])
            )
            override_error = evaluate(
                "absolute_error", predicted=float(entry.value), observed=recent_mean.result
            )
            if override_error.result > params.override_threshold.value:
                flags.append(
                    ProjectionFlag(
                        id=f"{player.id}:override:{entry.id}",
                        player_id=player.id,
                        field=entry.field,
                        kind="override",
                        model=float(entry.value),
                        observed=recent_mean.result,
                        reason="手調分鐘與近期實際不符",
                        traces=(recent_mean, override_error),
                    )
                )
    result: list[ProjectionFlag] = []
    for flag in flags:
        fingerprint = sha256(
            json.dumps(
                {
                    "flag": flag.model_dump(mode="json"),
                    "team": player.team_id,
                    "status": player.status,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:16]
        result.append(
            flag.model_copy(
                update={
                    "id": flag.id + ":" + fingerprint,
                    "suggestions": flag_suggestions(flag, params),
                }
            )
        )
    return tuple(result)


def flag_suggestions(flag: ProjectionFlag, params: InseasonParameters) -> dict[str, FormulaTrace]:
    target = "minutes" if flag.field == "minutes" else "rate:" + flag.field
    result: dict[str, FormulaTrace] = {}
    for field in params.fields:
        if target not in field.targets or not flag.traces:
            continue
        if field.kind == "override":
            result[field.id] = flag.traces[0]
        elif field.kind == "multiply" and flag.model > 0:
            result[field.id] = evaluate(
                "ratio", numerator=flag.observed, denominator=flag.model, zero_value=0.0
            )
    return result


def effective_projection(
    snapshot: PlayerSnapshot,
    priors: FrozenPriors,
    ledger: AdjustmentLedger,
    league: ProjectionRules,
    params: InseasonParameters,
    as_of: datetime,
    on: date,
) -> EffectiveProjection:
    if (
        priors.known_at > as_of
        or priors.season_id != snapshot.season_id
        or league.season_id != snapshot.season_id
    ):
        raise DataError("projection: prior is unavailable at decision time or season differs")
    players = visible_players(snapshot, as_of)
    zone = ZoneInfo(league.timezone)
    today = as_of.astimezone(zone).date()
    history = {
        pid: tuple(
            b
            for b in boxes
            if league.starts_on <= b.played_at.astimezone(zone).date() <= league.ends_on
        )
        for pid, boxes in observed_boxes(snapshot, as_of).items()
    }
    by_id = {p.player_id: p for p in priors.players}
    entries = active_entries(ledger, on, as_of)
    games = visible_games(snapshot, as_of)
    b2b_teams = back_to_back_teams(games, zone, on)
    result: list[EffectivePlayer] = []
    for player in players:
        boxes = history.get(player.id, ())
        prior = by_id.get(player.id) or fallback_prior(player, boxes, players, priors, params)
        needed_shots = {s.id for s in league.shots if s.made in required_statistics(league)}
        if needed_shots - prior.probabilities.keys():
            peer = fallback_prior(player, boxes, players, priors, params)
            prior = prior.model_copy(
                update={"probabilities": {**peer.probabilities, **prior.probabilities}}
            )
        status = effective_status(player, today, on)
        if status not in params.availability:
            raise DataError(f"parameters.availability.{status}: missing status probability")
        m, rates, shots, traces, weights, totals = blend_player(
            player, prior, boxes, league, params
        )
        own = tuple(e for e in entries if e.player_id == player.id)
        flags = player_flags(player, boxes, m, rates, own, params)
        b2b = player.team_id in b2b_teams
        m, q, rates, shots, adjustment_traces = adjusted_values(
            m, params.availability[status].value, rates, shots, own, params, b2b
        )
        traces.update(adjustment_traces)
        for shot in league.shots:
            if shot.id in shots:
                final_shot = evaluate(
                    "product", gain=rates[shot.attempted], probability=shots[shot.id]
                )
                rates[shot.made] = final_shot.result
                traces["final:" + shot.made] = final_shot
        distribution = priors.distribution
        if distribution is not None and distribution.scoring_stat in rates:
            trace = evaluate(
                "linear",
                values=tuple(rates[t.stat_id] for t in distribution.scoring_terms),
                weights=tuple(t.coefficient for t in distribution.scoring_terms),
            )
            rates[distribution.scoring_stat] = trace.result
            traces["final:" + distribution.scoring_stat] = trace
        expected: dict[str, float] = {}
        for stat, rate in rates.items():
            trace = evaluate("expectation", q=q, rate=rate, minutes=m)
            traces["expected:" + stat] = trace
            expected[stat] = trace.result
        for derived in league.derived:
            if derived.kind == "linear" and all(t.stat_id in expected for t in derived.terms):
                trace = evaluate(
                    "linear",
                    values=tuple(expected[t.stat_id] for t in derived.terms),
                    weights=tuple(t.coefficient for t in derived.terms),
                )
                expected[derived.id], traces[derived.id] = trace.result, trace
        result.append(
            EffectivePlayer(
                player=player,
                minutes=m,
                probability=q,
                rates=rates,
                probabilities=shots,
                expected=expected,
                prior=prior,
                observed_minutes=tuple(b.minutes for b in boxes),
                observed_dates=tuple(b.played_at.astimezone(zone).date() for b in boxes),
                current_totals=totals,
                current_minutes=fsum(b.minutes for b in boxes),
                weights=weights,
                traces=traces,
                adjustments=own,
                flags=flags,
            )
        )
    return EffectiveProjection(
        as_of=as_of, on=on, parameter_version=params.version, players=tuple(result)
    )
