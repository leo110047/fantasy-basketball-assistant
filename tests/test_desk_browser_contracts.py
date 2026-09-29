import subprocess
from pathlib import Path


def test_production_browser_handlers_preserve_inputs_and_reject_obsolete_results():
    root = Path(__file__).parents[1]
    result = subprocess.run(
        [
            "node",
            str(root / "tests/desk_browser_contracts.mjs"),
            str(root / "src/fba/apps/static"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
