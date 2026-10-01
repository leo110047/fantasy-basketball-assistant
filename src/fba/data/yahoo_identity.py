"""Unique identity resolution from documented Yahoo player metadata."""

import unicodedata

from fba.contracts.base import DataError
from fba.contracts.inseason import PlayerSnapshot
from fba.contracts.yahoo import IdentityMappings
from fba.data.yahoo import SyncBundle, text_at, xml


def name_key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def resolve_identities(
    bundle: SyncBundle,
    players: PlayerSnapshot,
    mappings: IdentityMappings,
) -> IdentityMappings:
    entries = mappings.entries.copy()
    stable = mappings.stable_entries.copy()
    available = {p.id for p in players.players}
    for key, raw in bundle.documents.items():
        if not key.startswith(("roster:", "players:")):
            continue
        for row in xml(raw.encode()).findall(".//player"):
            external = text_at(row, "player_key")
            editorial = row.findtext("editorial_player_key")
            known = entries.get(external) or (stable.get(editorial) if editorial else None)
            if known is not None:
                if known not in available:
                    raise DataError(
                        f"identity.{external}: mapped player {known} is absent from source"
                    )
                entries[external] = known
                if editorial:
                    stable[editorial] = known
                continue
            exact = tuple(
                p
                for p in players.players
                if editorial and p.provider_ids.get("yahoo_editorial") == editorial
            )
            name, team = row.findtext("name/full"), row.findtext("editorial_team_abbr")
            eligible = {p.text for p in row.findall("eligible_positions/position") if p.text}
            matching = exact or tuple(
                p
                for p in players.players
                if name
                and team
                and eligible
                and name_key(p.name) == name_key(name)
                and p.team_abbreviation is not None
                and p.team_abbreviation.casefold() == team.casefold()
                and eligible.intersection(p.positions)
            )
            if len(matching) == 1:
                entries[external] = matching[0].id
                if editorial:
                    stable[editorial] = matching[0].id
    return IdentityMappings(entries=entries, stable_entries=stable)


def unresolved_metadata(bundle: SyncBundle) -> dict[str, dict[str, str | None]]:
    return {
        text_at(row, "player_key"): {
            "name": row.findtext("name/full"),
            "team": row.findtext("editorial_team_abbr"),
            "editorial_key": row.findtext("editorial_player_key"),
        }
        for key, raw in bundle.documents.items()
        if key.startswith(("roster:", "players:"))
        for row in xml(raw.encode()).findall(".//player")
    }
