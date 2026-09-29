import platform
from pathlib import Path

import numpy as np
import pytest
from test_managed import small_arrays

import fba.adapters.native as native
from fba.contracts.auction import SolverError


@pytest.fixture
def native_package(tmp_path, monkeypatch):
    home = tmp_path / "fba"
    source = Path(native.__file__).parents[1] / "native/season.cpp"
    target = home / "native" / f"{platform.system().lower()}-{platform.machine().lower()}"
    target.mkdir(parents=True)
    (home / "native/season.cpp").write_bytes(source.read_bytes())
    monkeypatch.setattr(native, "__file__", str(home / "adapters/native.py"))
    return target / "season.so"


def test_packaged_library_matches_independent_fresh_build_and_workers(native_package):
    parent = native.NativeKernel()
    worker = None
    try:
        original = Path(parent.artifact.path).read_bytes()
        native_package.write_bytes(original)
        packaged = native.NativeKernel()
        try:
            assert packaged.artifact.path == str(native_package)
            assert native_package.read_bytes() == original
            assert Path(packaged.build.name, "season.so").read_bytes() == original
            worker = native.NativeKernel(packaged.artifact)
            assert worker.build is None and worker.artifact == packaged.artifact
            arrays = small_arrays()
            expected = parent(arrays, ((0,),), (1, 2))
            np.testing.assert_array_equal(packaged(arrays, ((0,),), (1, 2)), expected)
            np.testing.assert_array_equal(worker(arrays, ((0,),), (1, 2)), expected)
        finally:
            packaged.close()
    finally:
        if worker is not None:
            worker.close()
        parent.close()


def test_changed_package_is_rejected_before_load_and_temporary_build_is_cleaned(
    native_package, monkeypatch
):
    build, compiled = native.compile_kernel()
    native_package.write_bytes(compiled.read_bytes() + b"changed")
    monkeypatch.setattr(native, "compile_kernel", lambda: (build, compiled))

    def forbid_load(*args):
        pytest.fail("mismatched native bytes must not be loaded")

    monkeypatch.setattr(native.ctypes, "CDLL", forbid_load)
    with pytest.raises(SolverError, match="packaged native bytes differ"):
        native.NativeKernel()
    assert not Path(build.name).exists()


def test_packaged_file_does_not_bypass_required_compiler(native_package, monkeypatch):
    native_package.write_bytes(b"unverified")
    monkeypatch.setattr(native.shutil, "which", lambda _: None)
    with pytest.raises(SolverError, match="requires a local C\\+\\+17 compiler"):
        native.NativeKernel()
