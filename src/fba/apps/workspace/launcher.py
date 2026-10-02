import argparse
import multiprocessing
from pathlib import Path

from fba.apps.workspace.modes import ModeManager
from fba.apps.workspace.server import WorkspaceServer
from fba.runtime.local import (
    InstanceLock,
    data_directory,
    existing_url,
    launch_browser,
    open_existing,
    shutdown_signals,
)
from fba.runtime.processes import owned_processes


def serve_workspace(root: Path, *, open_browser: bool = True) -> None:
    lock = InstanceLock(root / "workspace.lock")
    if not lock.acquire():
        if open_browser:
            open_existing(lock, "workspace")
        else:
            existing_url(lock, "workspace")
        return
    try:
        modes = ModeManager(root)
        with owned_processes(), WorkspaceServer(modes) as server, shutdown_signals(server.stopping):
            lock.publish(server.instance)
            launch_browser(server, open_browser=open_browser)
            try:
                server.run()
            finally:
                modes.close()
    finally:
        lock.release()


def main() -> None:
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(
        prog="fba app", description="Open the shared basketball workspace"
    )
    parser.add_argument("--data", type=Path, default=data_directory())
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    serve_workspace(args.data.expanduser().resolve(), open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
