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
        self,
        collection: str,
        source: str,
        as_of: datetime,
        payload: JsonValue,
        *,
        identity: str | None = None,
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
                identities = None
                if identity is not None:
                    self.find_snapshot(collection, identity)
                    if path.exists():
                        refs = decode(SnapshotIndex, read_bytes(path), str(path))
                    identities = {**(refs.identities or {}), identity: sha}
                    if refs.identities and identity in refs.identities:
                        raise DataError(f"{collection}.identity: duplicate logical identity")
                atomic_write(
                    path,
                    canonical(SnapshotIndex(hashes=(*refs.hashes, sha), identities=identities)),
                )
            return sha

    def find_snapshot(self, collection: str, identity: str) -> StoredSnapshot | None:
        """Index logical IDs; migrate legacy indexes once without rewriting history."""
        path = self.root / f"{collection}.json"
        with self.lock:
            if not path.exists():
                return None
            refs = decode(SnapshotIndex, read_bytes(path), str(path))
            if refs.identities is None:
                identities: dict[str, str] = {}
                for sha in refs.hashes:
                    payload = self.load_snapshot(sha).payload
                    if not isinstance(payload, dict) or not isinstance(payload.get("id"), str):
                        raise DataError(f"{collection}.identity: snapshot has no string ID")
                    key = payload["id"]
                    assert isinstance(key, str)
                    if not key:
                        raise DataError(f"{collection}.identity: empty snapshot ID")
                    identities.setdefault(key, sha)  # Preserve the first actual recording.
                refs = refs.model_copy(update={"identities": identities})
                atomic_write(path, canonical(refs))
            sha = (refs.identities or {}).get(identity)
            if sha is None:
                return None
            if sha not in refs.hashes:
                raise DataError(f"{collection}.identity: digest is absent from history")
            record = self.load_snapshot(sha)
            if not isinstance(record.payload, dict) or record.payload.get("id") != identity:
                raise DataError(f"{collection}.identity: ID differs from indexed snapshot")
            return record

    def history(
        self, collection: str, *, limit: int | None = None, before: str | None = None
    ) -> tuple[StoredSnapshot, ...]:
        path = self.root / f"{collection}.json"
        if not path.exists():
            return ()
        refs = decode(SnapshotIndex, read_bytes(path), str(path))
        hashes = refs.hashes
        if before is not None:
            if before not in hashes:
                raise DataError(f"{collection}.before: unknown snapshot cursor")
            hashes = hashes[: hashes.index(before)]
        if limit is not None:
            if limit < 1:
                raise DataError(f"{collection}.limit: must be positive")
            hashes = hashes[-limit:]
        return tuple(self.load_snapshot(sha) for sha in hashes)


class SnapshotIndex(Record):
    hashes: tuple[Digest, ...]
    identities: dict[Text, Digest] | None = None
