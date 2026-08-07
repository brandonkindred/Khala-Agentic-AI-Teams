"""Tests for docker/redis/escape_requirepass.sh (compose requirepass escaping).

The redis:*-alpine image uses BusyBox: awk ``gsub(/\\\\/, ...)`` does not
double backslashes, which breaks AUTH for passwords containing ``\\``. The
compose entrypoint therefore shells out to this sed-based script.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCRIPT = _REPO_ROOT / "docker" / "redis" / "escape_requirepass.sh"
_ALPINE_IMAGE = "redis:7.4-alpine"


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


@pytest.mark.parametrize(
    "raw,expected",
    [
        (b"", b""),
        (b"please-change-me", b"please-change-me"),
        (b'p\\ass"x', b'p\\\\ass\\"x'),
        (b"a\tb", b"a\\tb"),
        (b"a\rb", b"a\\rb"),
        (b"line1\nline2", b"line1\\nline2"),
        (b'p\\ass"\t\r\nx', b'p\\\\ass\\"\\t\\r\\nx'),
    ],
)
def test_escape_requirepass_host(raw: bytes, expected: bytes) -> None:
    """Host sh/sed must match the redis.conf double-quoted escape contract."""
    assert _escape(raw, via="host") == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        (b'p\\ass"x', b'p\\\\ass\\"x'),
        (b"line1\nline2", b"line1\\nline2"),
        (b'p\\ass"\t\r\nx', b'p\\\\ass\\"\\t\\r\\nx'),
    ],
)
def test_escape_requirepass_busybox_alpine(raw: bytes, expected: bytes) -> None:
    """BusyBox in redis:7.4-alpine must double backslashes (the awk failure mode)."""
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    try:
        subprocess.run(
            ["docker", "image", "inspect", _ALPINE_IMAGE],
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip(f"{_ALPINE_IMAGE} not available locally")
    assert _escape(raw, via="alpine") == expected


def test_escape_requirepass_alpine_auth_smoke() -> None:
    """End-to-end: escaped conf + redis-server AUTH with a backslash password."""
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
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
    if result.returncode != 0 and "Unable to find image" in (result.stderr or ""):
        pytest.skip(f"{_ALPINE_IMAGE} not available locally")
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
