import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import JsonValue, TypeAdapter, ValidationError

from fba.contracts.base import DataError, Record
from fba.contracts.config import Source
from fba.contracts.data import Provenance
from fba.data.codec import digest, read_bytes


class Acquired(Record):
    source: Source
    provenance: Provenance
    data: bytes


def fetch(source: Source) -> bytes:
    headers = {
        "User-Agent": "fantasy-basketball-assistant/0.1",
        "Accept": "text/html"
        if source.adapter in ("espn_roster_page", "nba_release")
        else "application/json",
    }
    if source.adapter == "espn_players":
        headers["x-fantasy-filter"] = json.dumps(
            {"players": {"limit": 10000, "sortPercOwned": {"sortPriority": 1, "sortAsc": False}}}
        )
    try:
        with urlopen(Request(source.url, headers=headers), timeout=30) as response:
            if not response.url.startswith("https://"):
                raise DataError(f"{source.id}: redirected away from HTTPS")
            data = response.read(100_000_001)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise DataError(f"{source.id}: fetch failed for {source.url}: {exc}") from exc
    if not data or len(data) > 100_000_000:
        raise DataError(f"{source.id}: empty response or response exceeds 100 MB")
    if source.adapter == "espn_players":
        # A full page at this explicit bound may be truncated; never freeze it as complete.
        try:
            value = TypeAdapter[list[JsonValue] | dict[str, JsonValue]](
                list[JsonValue] | dict[str, JsonValue]
            ).validate_json(data)
        except ValidationError as exc:
            raise DataError(f"{source.id}: invalid ESPN response") from exc
        rows = value.get("players") if isinstance(value, dict) else value
        if not isinstance(rows, list) or len(rows) >= 10000:
            raise DataError(f"{source.id}: player response shape or pagination limit changed")
    return data


def acquire(source: Source, base: Path, cutoff: datetime) -> Acquired:
    data = (
        fetch(source) if source.delivery == "fetch" else read_bytes(base / str(source.manual_file))
    )
    retrieved = datetime.now(UTC)
    if source.delivery == "manual":
        capture = source.manual_capture
        if capture is None or digest(data) != capture.sha256:
            raise DataError(f"{source.id}: manual capture metadata or SHA-256 mismatch")
        retrieved = capture.retrieved_at
    if retrieved > cutoff:
        raise DataError(f"{source.id}: retrieval is after snapshot_as_of; use current cutoff")
    return Acquired(
        source=source,
        data=data,
        provenance=Provenance(
            source_id=source.id,
            url=source.url,
            raw_sha256=digest(data),
            available_as_of=source.available_as_of,
            retrieved_at=retrieved,
            delivery=source.delivery,
        ),
    )
