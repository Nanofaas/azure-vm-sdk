"""Run ruff, basedpyright and import-linter over the repository in sequence."""

from __future__ import annotations

import subprocess
import sys

CHECKS = (
    ("ruff", ["ruff", "check", "."]),
    ("basedpyright", ["basedpyright"]),
    ("import-linter", ["lint-imports"]),
)


def main() -> None:
    """Run every configured check and report which ones failed.

    Each check runs to completion regardless of earlier failures so all
    problems are reported at once. Writes ``Quality checks passed`` to stdout
    when every check exits zero.

    Raises:
        SystemExit: If any check exits non-zero; the message names them.

    """
    failures: list[str] = []
    for name, command in CHECKS:
        completed = subprocess.run(command, check=False)
        if completed.returncode != 0:
            failures.append(name)

    if failures:
        joined = ", ".join(failures)
        raise SystemExit(f"Quality checks failed: {joined}")

    sys.stdout.write("Quality checks passed\n")
