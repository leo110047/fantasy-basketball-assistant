import os
import signal
import subprocess
import sys
from time import monotonic, sleep

from test_service_recovery import native_child, process_exists


def test_forced_parent_death_terminates_active_spawn_worker_within_five_seconds(tmp_path):
    script = tmp_path / "worker_owner.py"
    script.write_text("""
import json, os, time
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from fba.runtime.processes import owned_processes, watch_parent

def active():
    print(json.dumps({'native_child': os.getpid()}), flush=True)
    while True:
        time.sleep(0.1)

if __name__ == '__main__':
    with owned_processes():
        with ProcessPoolExecutor(
            max_workers=1, mp_context=get_context('spawn'), initializer=watch_parent
        ) as pool:
            pool.submit(active).result()
""")
    process = subprocess.Popen(
        [sys.executable, "-u", str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=sys.platform != "win32",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
    )
    child = None
    try:
        child = native_child(process)
        assert process_exists(child)
        started = monotonic()
        process.kill()
        process.wait(timeout=5)
        while process_exists(child) and monotonic() - started < 5:
            sleep(0.02)
        assert not process_exists(child), "Worker survived the forced death of its owner"
        assert monotonic() - started < 5
    finally:
        if process.poll() is None:
            process.kill()
        if child is not None and process_exists(child):
            os.kill(child, signal.SIGTERM)
        process.communicate(timeout=5)
