# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A Pythonic SDK for managing Azure VMs through OpenTofu: a higher-level API over
a rendered OpenTofu workspace plus SSH access to the resulting machines. It is
consumed by `nanolab` through a git pin, so the public API in `azure_vm.__all__`
is a contract with that project.

## Setup

```bash
uv sync
```

Requires [uv](https://docs.astral.sh/uv/). Azure credentials and OpenTofu are
NOT required for the unit tests.

The toolchain (ruff, basedpyright, bandit, import-linter, pytest, pre-commit)
lives in `[dependency-groups].dev`, which a plain `uv sync` installs. It must
stay there and not move to `[project.optional-dependencies]`: extras are skipped
by `uv run`, so the basedpyright pre-commit hook — which runs `uv run --frozen
basedpyright` — would fail in CI with "Failed to spawn: basedpyright".

## Commands

```bash
# Tests (the integration marker is deselected by addopts)
uv run pytest
uv run pytest tests/unit/test_vm.py::test_name -v

# Integration tests - need real Azure credentials and OpenTofu
uv run pytest -m integration -v

# Lint, format, types, security, contracts - the same hooks CI runs
uv run ruff check .
uv run ruff format .
uv run basedpyright
uv run bandit -c pyproject.toml -r src
uv run lint-imports --config .importlinter --no-cache
uv run pre-commit run --all-files
```

`azure-vm-quality` runs ruff, basedpyright, bandit and import-linter together
and exits non-zero if any of them fails.

## Architecture

`src/azure_vm/` contains the package:

- `_backend.py` — the `TofuBackend` protocol and the subprocess implementation
  that shells out to `tofu`. All process execution goes through a backend, which
  is what makes the SDK testable without Azure.
- `_discovery.py` — locating the OpenTofu binary and the workspace root.
- `_templates.py` — rendering the `.tf` files for a VM.
- `_workspace.py` — the on-disk OpenTofu workspace: rendering, `init`, `apply`,
  output parsing.
- `models.py` — dataclasses (`VmConfig`, `VmInfo`, `VmSize`, `ImageInfo`,
  `VmState`, `VmArchitecture`, `CommandResult`, …).
- `exceptions.py` — typed hierarchy rooted at `AzureVmError`, plus
  `SshConnectionError` and the rest, so callers catch a specific failure rather
  than an opaque one.
- `vm.py` — `AzureVM`: the per-VM handle (info, start/stop/restart, delete,
  clone, `exec`, `exec_structured`, `transfer`, `wait_for_ip`, `wait_ready`).
  SSH goes through paramiko and commands are shell-quoted with `shlex.join`.
- `client.py` — `AzureClient`: cluster-level operations (launch, launch_many,
  get_vm, ensure_running, list, find, list_sizes, purge).
- `testing.py` — `FakeTofuBackend` for consumers writing their own tests.
- `e2e.py` — the `azure-vm-e2e` console script: an end-to-end lifecycle
  harness. Not imported by the library, and omitted from coverage.
- `devtools/` — repository tooling exposed as console scripts:
  `azure-vm-quality`, `azure-vm-package-report` (import graph metrics via
  `grimp`), `azure-vm-eval` (AST and coupling checks).

The architectural contracts live in `.importlinter`: `models` and `exceptions`
must not depend on the rest of the package, and the layers must not be imported
in the wrong direction. CI runs them, and so does pre-commit.

## Callers must not be broken

`nanolab` consumes this package through a git pin, so a change to a name in
`__all__`, to a signature, or to a returned model's attributes is a breaking
change for it even though nothing in this repository fails. Check that project
before renaming or removing a public symbol.

## Tooling

The same stack as the sibling projects.

| Concern | Tool | Config |
| --- | --- | --- |
| Lint + format | ruff (88 cols) | `[tool.ruff]` |
| Types | basedpyright (standard) | `[tool.basedpyright]` |
| Security | bandit | `[tool.bandit]` |
| Import layers | import-linter | `.importlinter` |
| Coverage | pytest-cov | `[tool.coverage.report]`, `fail_under = 80` |

Notes that are easy to get wrong:

- `SLF` (flake8-self) is part of the rule set here, as it was before the
  migration; the per-file-ignores record where private access is deliberate.
- This package supports Python 3.11, so `reportImplicitOverride` is off
  (`typing.override` needs 3.12).
- `uv.lock` is tracked, so CI can run `uv sync --frozen`. It used to be
  gitignored, which meant a clean checkout had no lockfile at all.
- `src/azure_vm/e2e.py` is omitted from coverage: it only runs against real
  Azure.
