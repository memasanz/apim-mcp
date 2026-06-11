"""Safe wrapper around the ``az`` CLI.

Design goals
------------
- Never use a shell; always invoke argv arrays.
- Reject parameter values that contain shell metacharacters or that try to
  smuggle additional commands via ``--query`` / ``--debug`` injection.
- Enforce per-call timeout and capture stdout/stderr separately.
- Force JSON output unless the caller asks for something else.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# Characters that have no legitimate place in an az CLI argument value.
# We intentionally include $ and % to block env-var expansion attempts the
# caller might try if the value were ever (mis)used in a shell.
_FORBIDDEN_CHARS = set(";&|`$><\n\r\t\x00%")
# Always-forbidden characters, even for "permissive" params (NUL and bare
# CR/LF would break argv handling or enable log injection).
_ALWAYS_FORBIDDEN_CHARS = set("\n\r\x00")

# Parameters the caller is never allowed to override — they are managed by
# the runner itself.
_RESERVED_PARAMS = frozenset({"--output", "-o", "--help", "-h", "--debug", "--verbose"})


class AzRunnerError(RuntimeError):
    """Base error for az runner failures."""


class AzValidationError(AzRunnerError):
    """Raised when caller-supplied parameters fail validation."""


class AzExecutionError(AzRunnerError):
    """Raised when the az process exits non-zero or times out."""

    def __init__(self, message: str, *, returncode: int | None, stdout: str, stderr: str) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@dataclass(slots=True)
class AzResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    json: Any = field(default=None)


def _validate_param_name(name: str) -> None:
    if not name.startswith("-"):
        raise AzValidationError(f"parameter name must start with '-': {name!r}")
    if name in _RESERVED_PARAMS:
        raise AzValidationError(f"parameter {name!r} is reserved by the runner")
    if not re.fullmatch(r"-{1,2}[A-Za-z0-9][A-Za-z0-9-]*", name):
        raise AzValidationError(f"invalid parameter name: {name!r}")


def _validate_param_value(value: str, *, permissive: bool = False) -> None:
    forbidden = _ALWAYS_FORBIDDEN_CHARS if permissive else _FORBIDDEN_CHARS
    if any(c in forbidden for c in value):
        raise AzValidationError(
            f"parameter value contains forbidden character(s): {value!r}"
        )
    # Permissive params (e.g. KQL) can be substantially larger.
    limit = 32768 if permissive else 4096
    if len(value) > limit:
        raise AzValidationError(f"parameter value exceeds {limit} characters")


def _coerce_value(value: Any) -> str:
    if isinstance(value, bool):
        # bool flags handled by caller; serialise defensively
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    raise AzValidationError(f"unsupported parameter value type: {type(value).__name__}")


def build_argv(
    az_command: list[str],
    params: dict[str, Any] | None = None,
    *,
    az_path: str = "az",
    output: str = "json",
    subscription: str | None = None,
    permissive_params: set[str] | None = None,
) -> list[str]:
    """Return a validated argv array for ``az``.

    ``params`` keys may be either ``--long`` or ``-s`` form. Boolean ``True``
    becomes a bare flag; ``False``/``None`` is dropped. Lists turn into
    repeated ``--name value`` pairs.
    """
    argv: list[str] = [az_path, *az_command]
    if subscription:
        _validate_param_value(subscription)
        argv += ["--subscription", subscription]
    argv += ["--output", output]

    permissive_set = permissive_params or set()
    for name, value in (params or {}).items():
        _validate_param_name(name)
        if value is None or value is False:
            continue
        if value is True:
            argv.append(name)
            continue
        is_permissive = name in permissive_set
        values = value if isinstance(value, list) else [value]
        for v in values:
            sval = _coerce_value(v)
            _validate_param_value(sval, permissive=is_permissive)
            argv += [name, sval]
    return argv


class AzRunner:
    """Executes ``az`` subprocesses safely."""

    def __init__(
        self,
        *,
        az_path: str | None = None,
        default_timeout: float = 60.0,
        default_subscription: str | None = None,
    ) -> None:
        resolved = az_path or shutil.which("az") or "az"
        self.az_path = resolved
        self.default_timeout = default_timeout
        self.default_subscription = default_subscription

    async def run(
        self,
        az_command: list[str],
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        subscription: str | None = None,
        output: str = "json",
        permissive_params: set[str] | None = None,
    ) -> AzResult:
        argv = build_argv(
            az_command,
            params,
            az_path=self.az_path,
            output=output,
            subscription=subscription or self.default_subscription,
            permissive_params=permissive_params,
        )

        log.info("running az: %s", " ".join(argv[1:]))  # don't log absolute path
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise AzExecutionError(
                f"az CLI not found at {self.az_path!r}",
                returncode=None,
                stdout="",
                stderr=str(exc),
            ) from exc

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout or self.default_timeout
            )
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise AzExecutionError(
                "az command timed out", returncode=None, stdout="", stderr=""
            ) from exc

        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            raise AzExecutionError(
                f"az exited with code {proc.returncode}",
                returncode=proc.returncode,
                stdout=stdout,
                stderr=stderr,
            )

        parsed: Any = None
        if output == "json" and stdout.strip():
            try:
                parsed = json.loads(stdout)
            except json.JSONDecodeError:
                log.warning("az returned non-JSON stdout despite --output json")
        return AzResult(
            args=argv[1:], returncode=proc.returncode, stdout=stdout, stderr=stderr, json=parsed
        )
