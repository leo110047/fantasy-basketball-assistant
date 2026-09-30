from pydantic import JsonValue

from fba.contracts.base import Record, Text
from fba.contracts.config import Category
from fba.contracts.inseason import DerivedStat, Shot


class YahooCatalog(Record):
    category_labels: dict[str, Category]
    stat_labels: dict[str, tuple[Text, ...]]
    positions: dict[str, tuple[Text, ...]]
    bench_labels: tuple[Text, ...]
    injury_labels: dict[str, tuple[Text, ...]]
    base_stats: tuple[Text, ...]
    shots: tuple[Shot, ...]
    derived: tuple[DerivedStat, ...]
    confirmation_defaults: dict[str, JsonValue]


class LeagueDraft(Record):
    document: dict[str, JsonValue]
    yahoo_settings: JsonValue
    confirm_fields: tuple[Text, ...]


class IdentityMappings(Record):
    entries: dict[str, Text]


class SettingsDifference(Record):
    key: Text
    local: JsonValue
    yahoo: JsonValue


class DiscoveredLeague(Record):
    key: Text
    game_key: Text
    season: Text
    name: Text
