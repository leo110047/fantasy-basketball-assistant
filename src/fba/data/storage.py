import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Literal

from pydantic import AwareDatetime, JsonValue

from fba.contracts.base import DataError, Record, Text
from fba.contracts.data import Digest
from fba.data.codec import canonical, decode, digest, read_bytes


class StoredSnapshot(Record):
    format_version: Literal[1]
    source: Text
    as_of: AwareDatetime
    sha256: Digest
    payload: JsonValue


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.lock = RLock()

    def snapshot(self, source: str, as_of: datetime, payload: JsonValue) -> str:
        data = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
        record = StoredSnapshot(
            format_version=1, source=source, as_of=as_of, sha256=digest(data), payload=payload
        )
        encoded = canonical(record)
        sha = digest(encoded)
        path = self.root / "snapshots" / f"{sha}.json"
        with self.lock:
            if path.exists():
                if read_bytes(path) != encoded:
                    raise DataError(f"snapshot.{sha}: existing content differs")
            else:
                atomic_write(path, encoded)
        return sha

    def load_snapshot(self, sha: str) -> StoredSnapshot:
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise DataError("snapshot.sha256: invalid digest")
        path = self.root / "snapshots" / f"{sha}.json"
        data = read_bytes(path)
        if digest(data) != sha:
            raise DataError(f"{path}: snapshot hash mismatch")
        record = decode(StoredSnapshot, data, str(path))
        payload = json.dumps(
            record.payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
        if digest(payload) != record.sha256:
            raise DataError(f"{path}: payload hash mismatch")
        return record

    def read[T: Record](self, name: str, model: type[T]) -> T:
        path = self.root / name
        return decode(model, read_bytes(path), str(path))

    def write(self, name: str, record: Record) -> None:
        with self.lock:
            atomic_write(self.root / name, canonical(record))

    def append_snapshot(
        self, collection: str, source: str, as_of: datetime, payload: JsonValue
    ) -> str:
        """Index publication is atomic; a crash can leave only an unreferenced complete snapshot."""
        with self.lock:
            sha = self.snapshot(source, as_of, payload)
            path = self.root / f"{collection}.json"
            refs = (
                decode(SnapshotIndex, read_bytes(path), str(path))
                if path.exists()
                else SnapshotIndex(hashes=())
            )
            if sha not in refs.hashes:
                atomic_write(path, canonical(SnapshotIndex(hashes=(*refs.hashes, sha))))
            return sha

    def history(self, collection: str) -> tuple[StoredSnapshot, ...]:
        path = self.root / f"{collection}.json"
        if not path.exists():
            return ()
        refs = decode(SnapshotIndex, read_bytes(path), str(path))
        return tuple(self.load_snapshot(sha) for sha in refs.hashes)


class SnapshotIndex(Record):
    hashes: tuple[Digest, ...]
