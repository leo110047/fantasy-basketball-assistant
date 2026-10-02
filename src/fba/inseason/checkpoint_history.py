"""Published-information checkpoints shared by projection fitting and evaluation."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fba.contracts.base import DataError
from fba.contracts.inseason import BoxScore
from fba.contracts.inseason_backtest import ProjectionStudySeason
from fba.inseason.projection import observed_boxes


@dataclass(frozen=True)
class Checkpoint:
    player_id: str
    as_of: datetime
    past: tuple[BoxScore, ...]
    future: tuple[BoxScore, ...]


def checkpoint_history(season: ProjectionStudySeason, count: int) -> tuple[Checkpoint, ...]:
    if count <= 0:
        raise DataError("backtest.checkpoint: positive observation count required")
    if (
        season.players.season_id != season.league.season_id
        or season.priors.season_id != season.league.season_id
    ):
        raise DataError("backtest.season: snapshot and prior must match the study season")
    zone = ZoneInfo(season.league.timezone)
    versions: dict[str, list[BoxScore]] = {}
    for box in season.players.boxes:
        if (
            season.league.starts_on
            <= box.played_at.astimezone(zone).date()
            <= season.league.ends_on
            and box.known_at <= season.players.as_of
        ):
            versions.setdefault(box.player_id, []).append(box)
    result: list[Checkpoint] = []
    for pid, rows in versions.items():
        first_publication: dict[str, datetime] = {}
        for box in rows:
            published = max(box.known_at, box.played_at)
            first_publication[box.game_id] = min(
                first_publication.get(box.game_id, published), published
            )
        if len(first_publication) <= count:
            continue
        # Later corrections never move the original decision time. Simultaneous
        # publications are all visible at that instant, even across the cutoff.
        at = sorted(first_publication.values())[count - 1] + timedelta(microseconds=1)
        if season.priors.known_at > at:
            raise DataError("backtest.prior: published after the checkpoint decision")
        player_history = season.players.model_copy(update={"boxes": tuple(rows)})
        past = observed_boxes(player_history, at).get(pid, ())
        final = observed_boxes(player_history, season.players.as_of).get(pid, ())
        future = tuple(b for b in final if b.played_at > at)
        if len(past) >= count and future:
            result.append(Checkpoint(pid, at, past, future))
    return tuple(result)
