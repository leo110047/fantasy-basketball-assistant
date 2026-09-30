import subprocess
import sys
from pathlib import Path


def main() -> int:
    for path in Path("src/fba").rglob("*.js"):
        result = subprocess.run(
            ["node", "--input-type=module", "--check"], input=path.read_bytes(), check=False
        )
        if result.returncode:
            print(f"JavaScript syntax failed: {path}", file=sys.stderr)
            return result.returncode
    imports = subprocess.run(
        [
            sys.executable,
            "-c",
            "from importlinter.cli import lint_imports_command; lint_imports_command()",
            "--no-cache",
        ],
        check=False,
    )
    if imports.returncode:
        return imports.returncode
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
