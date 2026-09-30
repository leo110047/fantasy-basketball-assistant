import argparse
import multiprocessing
from pathlib import Path

from fba.apps.inseason.server import serve_inseason
from fba.runtime.local import data_directory


def main() -> None:
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(prog="fba-inseason")
    parser.add_argument("--data", type=Path, default=data_directory())
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    serve_inseason(args.data, open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
