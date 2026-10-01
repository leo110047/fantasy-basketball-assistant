"""Record the Windows-only failures without replacing the complete verification.

This temporary CI probe inspects the current compiler's PE imports and the
venv launcher. It accesses no application data, credentials or providers.
"""

import _ctypes
import ctypes
import json
import subprocess
import sys
from pathlib import Path

from fba.adapters.native import compile_kernel, compiler_path


def interpreter_pids() -> tuple[int, int]:
    child = subprocess.Popen(
        [sys.executable, "-c", "import os; print(os.getpid())"], stdout=subprocess.PIPE
    )
    output, _ = child.communicate(timeout=10)
    if child.returncode:
        raise RuntimeError("the interpreter probe failed")
    return child.pid, int(output)


def main() -> int:
    if sys.platform != "win32":
        raise RuntimeError("the diagnostic requires the actual Windows runner")
    compiler = Path(compiler_path())
    version = subprocess.run(
        [str(compiler), "--version"], capture_output=True, text=True, check=True, timeout=10
    ).stdout.splitlines()[0]
    launcher_pid, interpreter_pid = interpreter_pids()
    report = {
        "diagnostic_only": True,
        "compiler": str(compiler),
        "compiler_version": version,
        "launcher_pid": launcher_pid,
        "interpreter_pid": interpreter_pid,
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
