from fba.contracts.base import DataError, FormatVersion, Record, Text
from fba.contracts.data import ProviderPlayer, ScheduleCount
from fba.data.codec import decode


class CountsFile(Record):
    format_version: FormatVersion
    season_id: Text
    counts: tuple[ScheduleCount, ...]


class RostersFile(Record):
    format_version: FormatVersion
    season_id: Text
    players: tuple[ProviderPlayer, ...]


def counts(data: bytes, season_id: str, source_id: str) -> tuple[ScheduleCount, ...]:
    parsed = decode(CountsFile, data, source_id)
    if parsed.season_id != season_id or any(c.source_id != source_id for c in parsed.counts):
        raise DataError(f"{source_id}: season or source ID mismatch")
    return tuple(sorted(parsed.counts, key=lambda c: c.team_id))


def rosters(data: bytes, season_id: str, source_id: str) -> tuple[ProviderPlayer, ...]:
    parsed = decode(RostersFile, data, source_id)
    if parsed.season_id != season_id:
        raise DataError(f"{source_id}: season mismatch")
    return tuple(sorted(parsed.players, key=lambda p: (p.provider, p.id)))
