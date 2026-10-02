"""Record the Windows-only failures without replacing the complete verification.

This temporary CI probe inspects the current compiler's PE imports and the
venv launcher. It accesses no application data, credentials or providers.
"""

import _ctypes
import ctypes
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from time import monotonic, sleep

from fba.adapters.native import compile_kernel, compiler_path


def interpreter_pids() -> tuple[int, int]:
    child = subprocess.Popen(
        [sys.executable, "-c", "import os; print(os.getpid())"], stdout=subprocess.PIPE
    )
    output, _ = child.communicate(timeout=10)
    if child.returncode:
        raise RuntimeError("the interpreter probe failed")
    return child.pid, int(output)


def process_api():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    for name, arguments, result in (
        ("OpenProcess", [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong], ctypes.c_void_p),
        ("WaitForSingleObject", [ctypes.c_void_p, ctypes.c_ulong], ctypes.c_ulong),
        ("TerminateProcess", [ctypes.c_void_p, ctypes.c_uint], ctypes.c_int),
        ("CloseHandle", [ctypes.c_void_p], ctypes.c_int),
    ):
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    return kernel


def launcher_cleanup(*, hold_launcher_open: bool = False) -> dict[str, object]:
    """Observe parent-only termination; retain a child handle for safe cleanup."""
    kernel = process_api()
    with tempfile.TemporaryDirectory(prefix="fba-launcher-probe-") as directory:
        pid_file = Path(directory) / "interpreter.pid"
        script = (
            "import os,sys,time; from pathlib import Path; p=Path(sys.argv[1]); "
            "t=p.with_suffix('.writing'); t.write_text(str(os.getpid())); t.replace(p); "
            "time.sleep(60)"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(pid_file)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        handle = None
        try:
            deadline = monotonic() + 5
            while not pid_file.exists():
                if monotonic() >= deadline or child.poll() is not None:
                    raise RuntimeError("the interpreter did not publish its PID")
                sleep(0.02)
            pid = int(pid_file.read_text())
            handle = kernel.OpenProcess(0x100001, False, pid)  # SYNCHRONIZE|TERMINATE
            if handle is None:
                raise ctypes.WinError(ctypes.get_last_error())
            if not hold_launcher_open:
                child.kill()  # Test the actual venv launcher's native job behavior.
            status = kernel.WaitForSingleObject(handle, 1000)
            if status not in (0, 258):  # WAIT_OBJECT_0 or WAIT_TIMEOUT.
                raise ctypes.WinError(ctypes.get_last_error())
            return {"launcher_pid": child.pid, "interpreter_pid": pid, "child_exited": status == 0}
        finally:
            if handle is not None:
                if kernel.WaitForSingleObject(handle, 0) != 0:
                    if not kernel.TerminateProcess(handle, 1):
                        raise ctypes.WinError(ctypes.get_last_error())
                kernel.CloseHandle(handle)
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)


def main() -> int:
    if sys.platform != "win32":
        raise RuntimeError("the diagnostic requires the actual Windows runner")
    compiler = Path(compiler_path())
    version = subprocess.run(
        [str(compiler), "--version"], capture_output=True, text=True, check=True, timeout=10
    ).stdout.splitlines()[0]
    launcher_pid, interpreter_pid = interpreter_pids()
    observations = {}
    for name, hold in (("forced_launcher_cleanup", False), ("survival_control", True)):
        try:
            observations[name] = launcher_cleanup(hold_launcher_open=hold)
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
            observations[name] = {"child_exited": None, "error": str(exc)}
    report = {
        "diagnostic_only": True,
        "compiler": str(compiler),
        "compiler_version": version,
        "launcher_pid": launcher_pid,
        "interpreter_pid": interpreter_pid,
        # The control keeps the launcher alive to exercise child-survival cleanup;
        # it does not claim a failure of the launcher's native job implementation.
        **observations,
    }
    build, path = compile_kernel()
    try:
        objdump = subprocess.run(
            [str(compiler), "-print-prog-name=objdump"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        imports = subprocess.run(
            [objdump, "-p", str(path)],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout
        report["dll_imports"] = [
            line.strip().removeprefix("DLL Name: ")
            for line in imports.splitlines()
            if line.strip().startswith("DLL Name: ")
        ]
        try:
            library = ctypes.CDLL(str(path))
        except OSError as exc:
            report["native_load_error"] = str(exc)
        else:
            _ctypes.FreeLibrary(library._handle)
            report["native_load_error"] = None
        print(json.dumps(report, indent=2))
    finally:
        build.cleanup()
    expected = (
        observations["forced_launcher_cleanup"]["child_exited"] is True
        and observations["survival_control"]["child_exited"] is False
    )
    return 0 if expected else 1


if __name__ == "__main__":
    raise SystemExit(main())
