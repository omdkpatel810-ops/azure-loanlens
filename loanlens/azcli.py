"""Running Azure CLI commands, for the drills that change Azure resources."""

from __future__ import annotations

import subprocess
from collections.abc import Callable

# run(args, stdin) -> (exit code, output)
Shell = Callable[[list[str], str | None], tuple[int, str]]


def run_shell(args: list[str], stdin: str | None = None) -> tuple[int, str]:  # pragma: no cover - real commands
    p = subprocess.run(args, input=stdin, capture_output=True, text=True, check=False)
    return p.returncode, p.stdout + p.stderr


def az(shell: Shell, args: list[str]) -> str:
    code, out = shell(["az", *args, "-o", "json"], None)
    if code != 0:
        raise RuntimeError(f"az {' '.join(args[:3])} failed: {out.strip()[-300:]}")
    return out.strip()
