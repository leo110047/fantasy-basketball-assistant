import hashlib
import json
from pathlib import Path

from pydantic import ValidationError

from fba.contracts.base import DataError, Record


def canonical(record: Record) -> bytes:
    return (
        json.dumps(
            record.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DataError(f"JSON: duplicate key {key}")
        result[key] = value
    return result


def checked_json(data: bytes, label: str) -> bytes:
    try:
        json.loads(data, object_pairs_hook=reject_duplicates)
    except (ValueError, UnicodeError) as exc:
        raise DataError(f"{label}: {exc}") from exc
    return data


def decode[T: Record](model: type[T], data: bytes, label: str) -> T:
    try:
        return model.model_validate_json(checked_json(data, label))
    except ValidationError as exc:
        raise DataError(f"{label}: {exc}") from exc


def read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise DataError(f"{path}: {exc.strerror}") from exc
