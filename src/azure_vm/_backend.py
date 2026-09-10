from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Protocol


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def success(self) -> bool:
        return self.returncode == 0


class CommandBackend(Protocol):
    def run(
        self,
        args: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult: ...


class TofuBackend:
    """Real backend — invokes the OpenTofu CLI via subprocess."""

    def run(
        self,
        args: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, cwd=cwd, env=env
            )
        except FileNotFoundError as err:
            from .exceptions import TofuNotInstalledError

            raise TofuNotInstalledError() from err
        return CommandResult(
            args=args,
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )


class FakeBackend:
    """Test backend — returns pre-configured responses and records all calls."""

    def __init__(
        self,
        responses: dict[tuple[str, ...], CommandResult] | None = None,
    ) -> None:
        self._responses: dict[tuple[str, ...], CommandResult] = responses or {}
        self._queues: dict[tuple[str, ...], list[CommandResult]] = {}
        self._calls: list[list[str]] = []
        self._cwds: list[str | None] = []
        self._envs: list[dict[str, str] | None] = []
        self._default: CommandResult | None = None

    def set_default(self, result: CommandResult) -> None:
        self._default = result

    def push(self, *args: str, result: CommandResult) -> None:
        self._queues.setdefault(args, []).append(result)

    def run(
        self,
        args: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        self._calls.append(list(args))
        self._cwds.append(cwd)
        self._envs.append(env)
        key = tuple(args)
        if self._queues.get(key):
            return self._queues[key].pop(0)
        if key in self._responses:
            return self._responses[key]
        if self._default is not None:
            return self._default
        raise KeyError(f"FakeBackend: no response configured for {args!r}")

    @property
    def calls(self) -> list[list[str]]:
        return list(self._calls)

    @property
    def cwds(self) -> list[str | None]:
        return list(self._cwds)

    def last_call(self) -> list[str]:
        return self._calls[-1] if self._calls else []

    def last_cwd(self) -> str | None:
        return self._cwds[-1] if self._cwds else None

    @property
    def envs(self) -> list[dict[str, str] | None]:
        return list(self._envs)

    def last_env(self) -> dict[str, str] | None:
        return self._envs[-1] if self._envs else None


def run_command(
    backend: CommandBackend,
    args: list[str],
    *,
    cwd: str | None = None,
) -> CommandResult:
    """Run *args* through *backend* and raise on failure."""
    from .exceptions import AzureVmCommandError

    result = backend.run(args, cwd=cwd)
    if not result.success:
        raise AzureVmCommandError(
            result.args, result.returncode, result.stdout, result.stderr
        )
    return result
