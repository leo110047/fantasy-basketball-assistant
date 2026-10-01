import subprocess
import sys
from pathlib import Path

from fba.adapters.native import compiler_path
from fba.adapters.native_formula import management_formula


def test_native_invariant_guards_reject_corrupt_internal_state(tmp_path):
    compiler = compiler_path()
    (tmp_path / "management-formula.h").write_text(management_formula())
    binary = tmp_path / ("native-guards.exe" if sys.platform == "win32" else "native-guards")
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-O2",
            "-ffp-contract=off",
            "-fno-access-control",
            "-I",
            str(tmp_path),
            str(Path(__file__).with_name("native_guards.cpp")),
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    subprocess.run([str(binary)], check=True, capture_output=True, timeout=10)
