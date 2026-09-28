import shutil
import subprocess
from pathlib import Path


def test_native_invariant_guards_reject_corrupt_internal_state(tmp_path):
    compiler = shutil.which("c++")
    assert compiler is not None
    binary = tmp_path / "native-guards"
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-O2",
            "-ffp-contract=off",
            "-fno-access-control",
            str(Path(__file__).with_name("native_guards.cpp")),
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    subprocess.run([str(binary)], check=True, capture_output=True, timeout=10)
