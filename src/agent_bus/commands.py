"""The one seam for running external tools (git, tmux).

Everything that shells out takes a `Runner` so tests substitute a fake and
never need the real binary. Callers catch `(OSError, subprocess.SubprocessError)`:
a missing binary raises OSError, a timeout raises TimeoutExpired.
"""

from __future__ import annotations

import os
import subprocess
from typing import Callable, Mapping

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def run_command(
    argv: list[str],
    *,
    timeout: float,
    env: Mapping[str, str] | None = None,
    cwd: str | os.PathLike | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
