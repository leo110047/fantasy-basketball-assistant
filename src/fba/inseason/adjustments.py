from datetime import date, datetime

from fba.contracts.base import DataError
from fba.contracts.inseason import AdjustmentEntry, AdjustmentLedger, EffectiveProjection
from fba.formulas.registry import evaluate


def active_entries(
    ledger: AdjustmentLedger, on: date, as_of: datetime
) -> tuple[AdjustmentEntry, ...]:
    visible = tuple(e for e in ledger.entries if e.created_at <= as_of)
    removed = {i for e in visible for i in e.revokes}
    for entry in reversed(visible):
        if entry.id not in removed and entry.replaces is not None:
            removed.add(entry.replaces)
    latest: dict[tuple[str, str], AdjustmentEntry] = {}
    for entry in visible:
        if not entry.revokes and entry.id not in removed and entry.starts_on <= on <= entry.ends_on:
            latest[entry.player_id, entry.field] = entry
    return tuple(latest[key] for key in sorted(latest))


def entry_state(entry: AdjustmentEntry, ledger: AdjustmentLedger, on: date, as_of: datetime) -> str:
    if entry.revokes:
        return "撤銷紀錄"
    visible = tuple(e for e in ledger.entries if e.created_at <= as_of)
    removed = {i for e in visible for i in e.revokes}
    for later in reversed(visible):
        if later.id not in removed and later.replaces is not None:
            removed.add(later.replaces)
    if entry.id in removed:
        return "已撤銷"
    if entry.ends_on < on:
        return "已到期"
    return "生效中" if entry.starts_on <= on and entry.created_at <= as_of else "未生效"


def redistribute(
    projection: EffectiveProjection, player_id: str, minutes: float
) -> dict[str, float]:
    player = next((p for p in projection.players if p.player.id == player_id), None)
    if player is None:
        raise DataError(f"redistribution.{player_id}: unknown player")
    teammates = tuple(
        p
        for p in projection.players
        if p.player.team_id == player.player.team_id and p.player.id != player_id
    )
    total = sum(p.minutes for p in teammates)
    delta = minutes - player.minutes
    if total == 0 or delta > total or minutes < 0:
        raise DataError("redistribution.minutes: teammates cannot supply the requested minutes")
    return {
        player_id: minutes,
        **{
            p.player.id: evaluate(
                "reallocate", current=p.minutes, delta=delta, team_total=total
            ).result
            for p in teammates
        },
    }
