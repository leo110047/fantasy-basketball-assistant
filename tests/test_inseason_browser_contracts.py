import subprocess
from pathlib import Path


def test_job_completion_and_failure_restore_current_trade_controls():
    root = Path(__file__).parents[1]
    result = subprocess.run(
        [
            "node",
            str(root / "tests/inseason_browser_contracts.mjs"),
            str(root / "src/fba/apps/inseason/static"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
