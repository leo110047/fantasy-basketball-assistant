import subprocess
import sys


def main() -> int:
    commands = (
        ("ruff", "check", "--no-cache", "src", "tests", "scripts"),
        ("ruff", "format", "--check", "--no-cache", "src", "tests", "scripts"),
        ("basedpyright", "--pythonpath", sys.executable),
        ("vulture", "src", "--min-confidence", "80"),
        ("deptry", "src"),
        ("pytest", "-q", "-p", "no:cacheprovider"),
    )
    for command in commands:
        result = subprocess.run((sys.executable, "-m", *command), check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
