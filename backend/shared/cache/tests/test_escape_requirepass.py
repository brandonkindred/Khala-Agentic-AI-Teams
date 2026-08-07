"""Tests for docker/redis/escape_requirepass.sh (compose requirepass escaping).

Encodes every password byte as redis.conf ``\\xHH`` so BusyBox Alpine and host
userland round-trip the same value (including backslash and trailing newline).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCRIPT = _REPO_ROOT / "docker" / "redis" / "escape_requirepass.sh"
# Keep in sync with compose Redis image and the CI pull step in test-shared-cache.
_ALPINE_IMAGE = "redis:7.4-alpine"


def _alpine_ready() -> str | None:
    """Return a skip/fail reason when Docker + the Alpine image are unavailable."""
    if shutil.which("docker") is None:
        return "docker not available"
    try:
        subprocess.run(
            ["docker", "image", "inspect", _ALPINE_IMAGE],
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
        return f"{_ALPINE_IMAGE} not available locally ({exc})"
    # Probe the daemon — ``image inspect`` can succeed from a stale cache while
    # ``docker run`` fails with permission denied / daemon down.
    probe = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if probe.returncode != 0:
        return f"docker daemon not usable: {(probe.stderr or probe.stdout or '').strip()}"
    return None


def _require_alpine() -> None:
    """Skip locally when Alpine Redis is unavailable; fail hard under CI.

    Preconditions:
        - None.
    Postconditions:
        - Returns when Docker can run ``_ALPINE_IMAGE``.
        - Under ``CI`` (truthy), missing image/daemon is a hard failure so the
          BusyBox encode + AUTH smoke paths cannot silently skip in CI.
        - Outside CI, missing Docker/image skips (developer laptops).
    """
    reason = _alpine_ready()
    if reason is None:
        return
    if os.getenv("CI", "").strip():
        pytest.fail(f"Alpine Redis required in CI for requirepass escape tests: {reason}")
    pytest.skip(reason)


def _escape(password: bytes, *, via: str = "host") -> bytes:
    """Run the escape script on *password*.

    Preconditions:
        - ``via`` is ``\"host\"`` (local ``sh``) or ``\"alpine\"`` (BusyBox in
          ``redis:7.4-alpine``).
    Postconditions:
        - Returns the script stdout bytes (no forced trailing newline).
    """
    assert _SCRIPT.is_file(), f"missing escape script at {_SCRIPT}"
    if via == "host":
        return subprocess.check_output(["sh", str(_SCRIPT)], input=password)
    assert via == "alpine"
    return subprocess.check_output(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "-v",
            f"{_SCRIPT}:/escape_requirepass.sh:ro",
            _ALPINE_IMAGE,
            "/bin/sh",
            "/escape_requirepass.sh",
        ],
        input=password,
    )


def _hex_escape(raw: bytes) -> bytes:
    """Reference encoding matching escape_requirepass.sh (``\\xHH`` per byte)."""
    return "".join(f"\\x{b:02x}" for b in raw).encode("ascii")


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"please-change-me",
        b'p\\ass"x',
        b"a\tb",
        b"a\rb",
        b"line1\nline2",
        b"abc\n",
        b'p\\ass"\t\r\nx',
    ],
)
def test_escape_requirepass_host(raw: bytes) -> None:
    """Host sh/od/sed must emit redis.conf ``\\xHH`` escapes for every byte."""
    assert _escape(raw, via="host") == _hex_escape(raw)


@pytest.mark.parametrize(
    "raw",
    [
        b'p\\ass"x',
        b"line1\nline2",
        b"abc\n",
        b'p\\ass"\t\r\nx',
    ],
)
def test_escape_requirepass_busybox_alpine(raw: bytes) -> None:
    """BusyBox in redis:7.4-alpine must match the host hex encoding."""
    _require_alpine()
    assert _escape(raw, via="alpine") == _hex_escape(raw)


def test_escape_requirepass_alpine_auth_smoke() -> None:
    """End-to-end: escaped conf + redis-server AUTH with a backslash password."""
    _require_alpine()
    password = r"p\ass\"word"
    # Build conf inside Alpine the same way compose does, then AUTH.
    script = r"""
set -eu
esc=$(printf '%s' "$REDIS_PASSWORD" | /bin/sh /escape_requirepass.sh)
conf=/tmp/khala-redis.conf
cat > "$conf" <<EOF
bind 127.0.0.1
port 6379
requirepass "$esc"
save ""
appendonly no
EOF
redis-server "$conf" --daemonize yes
# Wait until ready
i=0
while [ "$i" -lt 50 ]; do
  if redis-cli -a "$REDIS_PASSWORD" --no-auth-warning ping 2>/dev/null | grep -q PONG; then
    exit 0
  fi
  i=$((i + 1))
  sleep 0.1
done
echo "AUTH failed; conf:" >&2
cat "$conf" >&2
redis-cli -a "$REDIS_PASSWORD" --no-auth-warning ping >&2 || true
exit 1
"""
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-e",
            f"REDIS_PASSWORD={password}",
            "-e",
            f"REDISCLI_AUTH={password}",
            "-v",
            f"{_SCRIPT}:/escape_requirepass.sh:ro",
            _ALPINE_IMAGE,
            "/bin/sh",
            "-c",
            script,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
