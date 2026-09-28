import ctypes
import errno
import os
import sys
import tempfile
from pathlib import Path
from typing import Protocol, cast

from fba.adapters.codec import canonical, read_bytes
from fba.contracts.auction import DraftState
from fba.contracts.base import DataError
from fba.contracts.desk import DeskExecution


class NativeExchange(Protocol):
    def __call__(self, *args: object) -> int: ...


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
        with tempfile.NamedTemporaryFile(dir=history, suffix=".json", delete=False) as f:
            temporary = Path(f.name)
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        if read_bytes(path) != expected:
            raise DataError("draft: file changed outside this session; restart and inspect it")
        atomic_exchange(temporary, path)
        exchanged = True
    except OSError as exc:
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
