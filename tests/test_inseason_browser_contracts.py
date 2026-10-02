import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("script", ["inseason_browser_contracts.mjs", "inseason_daily_ui.mjs"])
def test_job_completion_and_failure_restore_current_trade_controls(script):
    root = Path(__file__).parents[1]
    result = subprocess.run(
        [
            "node",
            str(root / "tests" / script),
            str(root / "src/fba/apps/inseason/static"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
