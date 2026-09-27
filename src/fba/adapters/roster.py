import csv
import io

from pydantic import ValidationError

from fba.contracts.base import DataError
from fba.contracts.config import RosterImport
from fba.contracts.data import RosterRow


def quote(value: str, label: str) -> float | None:
    if not value.strip() or value.strip() == "-":
        return None
    try:
        return float(value.strip().removeprefix("$"))
    except ValueError as exc:
        raise DataError(f"{label}: invalid quote {value!r}") from exc


def import_roster(
    data: bytes, config: RosterImport, positions: tuple[str, ...]
) -> tuple[RosterRow, ...]:
    try:
        text = data.decode(config.encoding)
    except (UnicodeError, LookupError) as exc:
        raise DataError(f"{config.path}: invalid encoding {config.encoding}") from exc
    columns = config.columns
    reader = csv.DictReader(io.StringIO(text), delimiter=config.delimiter, strict=True)
    expected = {str(v) for v in columns.model_dump().values()}
    if reader.fieldnames is None or not expected <= set(reader.fieldnames):
        raise DataError(
            f"{config.path}: missing columns {sorted(expected - set(reader.fieldnames or []))}"
        )
    if len(reader.fieldnames) != len(set(reader.fieldnames)):
        raise DataError(f"{config.path}: duplicate column headers")
    rows: list[RosterRow] = []
    try:
        for number, row in enumerate(reader, start=2):
            label = f"{config.path}:{number}"
            if None in row or any(v is None for v in row.values()):
                raise DataError(f"{label}: wrong number of columns")
            parsed = RosterRow(
                id=row[columns.player_id].strip(),
                name=row[columns.name].strip(),
                positions=tuple(sorted(p.strip() for p in row[columns.positions].split(","))),
                rank=int(row[columns.rank]),
                projected_price=quote(row[columns.projected_price], label),
                average_price=quote(row[columns.average_price], label),
            )
            if len(set(parsed.positions)) != len(parsed.positions) or (
                set(parsed.positions) - set(positions)
            ):
                raise DataError(f"{label}: unknown or duplicate positions")
            rows.append(parsed)
    except (ValueError, csv.Error, ValidationError) as exc:
        raise DataError(f"{config.path}:{reader.line_num}: {exc}") from exc
    if not rows or len({r.id for r in rows}) != len(rows):
        raise DataError(f"{config.path}: empty roster or duplicate player IDs")
    return tuple(sorted(rows, key=lambda r: r.id))
