from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

from azure_vm.devtools import quality


def _install_fake_tool(
    bin_dir: Path, name: str, exit_code: int, log_env: str = "AZURE_VM_TEST_LOG"
) -> None:
    """Drop an executable named ``name`` on PATH that logs argv and exits."""
    script = bin_dir / name
    script.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "import sys\n"
        f"with open(os.environ['{log_env}'], 'a') as fh:\n"
        "    fh.write(repr(sys.argv) + '\\n')\n"
        f"sys.exit({exit_code})\n"
    )
    script.chmod(0o755)


def _invocations(log: Path) -> list[list[str]]:
    """Return the logged argv lists, argv[0] reduced to the bare tool name."""
    calls = []
    for line in log.read_text().splitlines():
        argv = ast.literal_eval(line)
        calls.append([Path(argv[0]).name, *argv[1:]])
    return calls


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """Provide a PATH whose ruff/basedpyright/lint-imports are loggers."""

    def install(exit_codes: dict[str, int]) -> Path:
        log = tmp_path / "calls.log"
        monkeypatch.setenv("AZURE_VM_TEST_LOG", str(log))
        monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
        for name, code in exit_codes.items():
            _install_fake_tool(tmp_path, name, code)
        return log

    return install


def test_main_runs_each_check_with_its_configured_command(harness, capsys):
    log = harness({"ruff": 0, "basedpyright": 0, "lint-imports": 0})

    result = quality.main()

    assert result is None
    assert _invocations(log) == [
        ["ruff", "check", "."],
        ["basedpyright"],
        ["lint-imports"],
    ]
    captured = capsys.readouterr()
    assert captured.out == "Quality checks passed\n"
    assert captured.err == ""


def test_main_names_the_single_failed_check(harness, capsys):
    harness({"ruff": 1, "basedpyright": 0, "lint-imports": 0})

    with pytest.raises(SystemExit) as excinfo:
        quality.main()

    assert str(excinfo.value) == "Quality checks failed: ruff"
    # Nothing is reported as passing when a check fails.
    assert capsys.readouterr().out == ""


def test_main_keeps_going_and_reports_every_failed_check_in_order(harness):
    log = harness({"ruff": 1, "basedpyright": 0, "lint-imports": 1})

    with pytest.raises(SystemExit) as excinfo:
        quality.main()

    # Failures are named in CHECKS order, not in the order they failed in, and
    # a non-zero check does not abort the ones after it.
    assert str(excinfo.value) == "Quality checks failed: ruff, import-linter"
    assert _invocations(log) == [
        ["ruff", "check", "."],
        ["basedpyright"],
        ["lint-imports"],
    ]


def test_main_names_a_failure_that_is_not_the_first_check(harness):
    harness({"ruff": 0, "basedpyright": 2, "lint-imports": 0})

    with pytest.raises(SystemExit) as excinfo:
        quality.main()

    assert str(excinfo.value) == "Quality checks failed: basedpyright"
