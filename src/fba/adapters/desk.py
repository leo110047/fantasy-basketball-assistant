import ctypes
import errno
import os
import sys
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol, cast

from fba.adapters.codec import canonical, read_bytes
from fba.contracts.auction import DraftState
from fba.contracts.base import DataError
from fba.contracts.desk import DeskExecution, SaveUnconfirmed


class NativeExchange(Protocol):
    def __call__(self, *args: object) -> int: ...


@contextmanager
def draft_lock(path: Path) -> Generator[None]:
    if sys.platform not in ("darwin", "linux"):
        raise DataError("serve: draft storage requires macOS or Linux")
    import fcntl

    try:
        lock = path.with_suffix(path.suffix + ".lock").open("a")
    except OSError as exc:
        raise DataError(f"serve: cannot open draft lock: {exc}") from exc
    with lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise DataError(f"serve: cannot exclusively lock draft: {exc}") from exc
        yield


def check_storage(path: Path) -> None:
    """Probe the real destination filesystem without modifying the draft."""
    try:
        history = path.parent / f".{path.name}.history"
        history.mkdir(mode=0o700, exist_ok=True)
        with (
            tempfile.TemporaryDirectory(dir=path.parent, prefix=".fba-storage-") as directory,
            tempfile.TemporaryDirectory(dir=history, prefix=".probe-") as archive,
        ):
            first, second = Path(directory) / "first", Path(archive) / "second"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            atomic_exchange(first, second)
            if first.read_bytes() != b"second" or second.read_bytes() != b"first":
                raise DataError("serve: filesystem failed atomic draft exchange probe")
            os.link(first, Path(archive) / "history")
            sync_directory(history)
            sync_directory(path.parent)
    except OSError as exc:
        raise DataError(f"serve: filesystem cannot atomically save drafts: {exc}") from exc


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_exchange(source: Path, target: Path) -> None:
    """Swap names atomically, retaining the displaced inode even across external writes."""
    library = ctypes.CDLL(None, use_errno=True)
    a, b = os.fsencode(source), os.fsencode(target)
    try:
        if sys.platform == "darwin":
            function = library.renamex_np
            function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            arguments = (a, b, 2)  # RENAME_SWAP
        elif sys.platform == "linux":
            function = library.renameat2
            function.argtypes = [ctypes.c_int, ctypes.c_char_p] * 2 + [ctypes.c_uint]
            arguments = (-100, a, -100, b, 2)  # AT_FDCWD, RENAME_EXCHANGE
        else:
            raise OSError(errno.ENOTSUP, "atomic draft exchange requires macOS or Linux")
    except AttributeError as exc:
        raise OSError(errno.ENOTSUP, "atomic draft exchange is unavailable") from exc
    function.restype = ctypes.c_int
    if cast(NativeExchange, function)(*arguments):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def save_draft(path: Path, state: DraftState, expected: bytes) -> bytes:
    """Atomically save and retain the displaced version in the draft's history directory.

    The caller holds the canonical sibling .lock; history also preserves edits
    from writers ignoring that lock. No history file is deleted automatically.
    """
    payload = canonical(state)
    temporary: Path | None = None
    exchanged = False
    try:
        history = path.parent / f".{path.name}.history"
        history.mkdir(mode=0o700, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=history, suffix=".pending", delete=False) as f:
            temporary = Path(f.name)
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        if read_bytes(path) != expected:
            raise DataError("draft: file changed outside this session; restart and inspect it")
        atomic_exchange(temporary, path)
        exchanged = True
        # Publish history only after exchange; interrupted staging is never a committed backup.
        os.link(temporary, temporary.with_suffix(".json"))
        temporary.unlink()
        sync_directory(history)
        sync_directory(path.parent)
    except OSError as exc:
        if exchanged:
            raise SaveUnconfirmed(
                f"draft replaced; history or durability unconfirmed, reload before retrying: {exc}"
            ) from exc
        raise DataError(f"draft: could not persist {path}: {exc}") from exc
    finally:
        if temporary is not None and not exchanged:
            temporary.unlink(missing_ok=True)
    return payload


def log_execution(path: Path, record: DeskExecution) -> None:
    try:
        with path.open("ab") as stream:
            stream.write(canonical(record))
            stream.flush()
    except OSError as exc:
        raise DataError(f"desk.log: cannot record calculation: {exc}") from exc
