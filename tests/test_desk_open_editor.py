import os
import sys

import pytest
from test_desk import desk as desk
from test_desk import sell_request

from fba.contracts.base import DataError
from fba.data.codec import canonical


def open_editor(path, share_delete):
    if sys.platform != "win32" or not share_delete:
        return path.open("r+b")
    import ctypes
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    create.restype = ctypes.c_void_p
    # GENERIC_READ|WRITE; FILE_SHARE_READ|WRITE|DELETE; OPEN_EXISTING.
    handle = create(str(path), 0xC0000000, 7, None, 3, 0, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
    except OSError:
        close = kernel.CloseHandle
        close.argtypes = [ctypes.c_void_p]
        close.restype = ctypes.c_int
        close(handle)
        raise
    return os.fdopen(descriptor, "r+b")  # The stream now owns the native handle.


@pytest.mark.parametrize("share_delete", [False, True])
def test_open_editor_can_finish_writing_displaced_version_after_save(desk, share_delete):
    external = canonical(desk.bootstrap().desk.state.model_copy(update={"revision": 99}))
    original = desk.path.read_bytes()
    request = sell_request(desk)
    history = desk.path.parent / f".{desk.path.name}.history"
    blocked = sys.platform == "win32" and not share_delete
    with open_editor(desk.path, share_delete) as editor:
        if blocked:
            # Windows cannot replace a handle that denies DELETE sharing.
            with pytest.raises(DataError, match="could not persist"):
                desk.save(request)
            assert desk.path.read_bytes() == original
            assert canonical(desk.bootstrap().desk.state) == original
            assert not tuple(history.iterdir())
        else:
            saved = desk.save(request)
            editor.write(external)
            editor.truncate()
    if blocked:
        saved = desk.save(request)  # The unchanged request remains valid after close.
    assert desk.path.read_bytes() == canonical(saved.state)
    backups = list(history.glob("*.json"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == (original if blocked else external)
