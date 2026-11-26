"""The command runner seam."""

from __future__ import annotations

import os
import sys

from agent_bus import commands


def test_run_command_uses_the_given_cwd(tmp_path):
    result = commands.run_command(
        [sys.executable, "-c", "import os; print(os.getcwd())"], timeout=30, cwd=tmp_path
    )
    assert os.path.realpath(result.stdout.strip()) == os.path.realpath(tmp_path)
