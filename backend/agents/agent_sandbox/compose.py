"""Thin asyncio wrapper around the ``docker compose`` CLI.

Isolated in its own module so tests can mock it cleanly via patching.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class ComposeError(RuntimeError):
    """Raised when a ``docker compose`` invocation exits non-zero."""

    def __init__(self, command: list[str], exit_code: int, stderr: str) -> None:
        self.command = command
        self.exit_code = exit_code
        self.stderr = stderr
        super().__init__(
            f"docker compose failed (exit {exit_code}): {' '.join(command)}\n{stderr[:500]}"
        )


async def run_compose(
    compose_file: Path, args: list[str], *, timeout_s: int = 120
) -> tuple[int, str, str]:
    """Run ``docker compose -f <compose_file> <args...>`` and return (rc, stdout, stderr)."""
    cmd = ["docker", "compose", "-f", str(compose_file), *args]
    logger.debug("exec: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ComposeError(cmd, -1, f"command timed out after {timeout_s}s") from None
    return (
        proc.returncode or 0,
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
    )


async def up_detached(compose_file: Path, service: str, *, timeout_s: int = 180) -> None:
    """Run ``docker compose up -d <service>`` raising ComposeError on failure."""
    rc, out, err = await run_compose(compose_file, ["up", "-d", service], timeout_s=timeout_s)
    if rc != 0:
        raise ComposeError(["up", "-d", service], rc, err or out)


async def stop_and_remove(compose_file: Path, service: str, *, timeout_s: int = 60) -> None:
    """Stop and remove ``service`` (but keep volumes). Raises on failure."""
    rc, out, err = await run_compose(
        compose_file, ["rm", "-s", "-f", "-v", service], timeout_s=timeout_s
    )
    if rc != 0:
        raise ComposeError(["rm", "-s", "-f", "-v", service], rc, err or out)


async def is_running(compose_file: Path, service: str) -> bool:
    """Return True if ``service`` is in ``docker compose ps`` with a running state."""
    rc, out, err = await run_compose(
        compose_file, ["ps", "--services", "--filter", "status=running"], timeout_s=15
    )
    if rc != 0:
        logger.warning("compose ps failed: %s", err)
        return False
    return service in {line.strip() for line in out.splitlines() if line.strip()}
