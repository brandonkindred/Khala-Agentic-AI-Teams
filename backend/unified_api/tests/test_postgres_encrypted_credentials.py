"""Unit tests for unified_api.postgres_encrypted_credentials."""

import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_backend = Path(__file__).resolve().parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))


def _reload(monkeypatch: pytest.MonkeyPatch, postgres_host: str = ""):
    """Reload the module with the given POSTGRES_HOST environment variable."""
    monkeypatch.setenv("POSTGRES_HOST", postgres_host)
    import unified_api.postgres_encrypted_credentials as mod

    importlib.reload(mod)
    return mod


# ---------------------------------------------------------------------------
# postgres_credentials_enabled
# ---------------------------------------------------------------------------


def test_postgres_credentials_enabled_false_when_no_host(monkeypatch: pytest.MonkeyPatch):
    """postgres_credentials_enabled() returns False when POSTGRES_HOST is not set."""
    mod = _reload(monkeypatch, postgres_host="")
    assert mod.postgres_credentials_enabled() is False


def test_postgres_credentials_enabled_false_when_whitespace(monkeypatch: pytest.MonkeyPatch):
    """postgres_credentials_enabled() returns False when POSTGRES_HOST is only whitespace."""
    monkeypatch.setenv("POSTGRES_HOST", "   ")
    mod = _reload(monkeypatch, postgres_host="   ")
    assert mod.postgres_credentials_enabled() is False


def test_postgres_credentials_enabled_true_when_host_set(monkeypatch: pytest.MonkeyPatch):
    """postgres_credentials_enabled() returns True when POSTGRES_HOST has a non-empty value."""
    mod = _reload(monkeypatch, postgres_host="localhost")
    assert mod.postgres_credentials_enabled() is True


# ---------------------------------------------------------------------------
# _dsn
# ---------------------------------------------------------------------------


def test_dsn_builds_correct_url(monkeypatch: pytest.MonkeyPatch):
    """_dsn() assembles a postgresql:// URL from environment variables."""
    monkeypatch.setenv("POSTGRES_HOST", "myhost")
    monkeypatch.setenv("POSTGRES_USER", "myuser")
    monkeypatch.setenv("POSTGRES_PASSWORD", "mypassword")
    monkeypatch.setenv("POSTGRES_DB", "mydb")
    monkeypatch.setenv("POSTGRES_PORT", "5433")
    mod = _reload(monkeypatch, postgres_host="myhost")
    dsn = mod._dsn()
    assert "postgresql://" in dsn
    assert "myhost" in dsn
    assert "myuser" in dsn
    assert "mydb" in dsn
    assert "5433" in dsn


def test_dsn_uses_defaults_for_optional_vars(monkeypatch: pytest.MonkeyPatch):
    """_dsn() uses sensible defaults when optional env vars are absent."""
    monkeypatch.setenv("POSTGRES_HOST", "host")
    monkeypatch.delenv("POSTGRES_USER", raising=False)
    monkeypatch.delenv("POSTGRES_DB", raising=False)
    monkeypatch.delenv("POSTGRES_PORT", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    mod = _reload(monkeypatch, postgres_host="host")
    dsn = mod._dsn()
    # Defaults: user=postgres, db=postgres, port=5432
    assert "postgres" in dsn
    assert "5432" in dsn


def test_dsn_url_encodes_special_chars_in_password(monkeypatch: pytest.MonkeyPatch):
    """_dsn() percent-encodes special characters in the password."""
    monkeypatch.setenv("POSTGRES_HOST", "host")
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss:word!")
    mod = _reload(monkeypatch, postgres_host="host")
    dsn = mod._dsn()
    # Raw password must not appear; @ must be encoded
    assert "p@ss:word!" not in dsn
    assert "p%40ss" in dsn  # @ -> %40


def test_dsn_bounds_connect_timeout(monkeypatch: pytest.MonkeyPatch):
    """The credential-store DSN carries connect_timeout (from the shared helper) so a
    PAT read can't hang on a down host before any reachability probe runs."""
    monkeypatch.setenv("POSTGRES_HOST", "host")
    monkeypatch.delenv("POSTGRES_CONNECT_TIMEOUT_S", raising=False)
    mod = _reload(monkeypatch, postgres_host="host")
    assert "connect_timeout=3" in mod._dsn()
    monkeypatch.setenv("POSTGRES_CONNECT_TIMEOUT_S", "9")
    assert "connect_timeout=9" in mod._dsn()


def test_dsn_encodes_space_password_as_percent20(monkeypatch: pytest.MonkeyPatch):
    """A space in the password must percent-encode to %20, NOT '+' (quote, not
    quote_plus): in a URI userinfo libpq treats '+' as a literal plus, so quote_plus
    would authenticate with the wrong password."""
    monkeypatch.setenv("POSTGRES_HOST", "host")
    monkeypatch.setenv("POSTGRES_PASSWORD", "my pass")
    mod = _reload(monkeypatch, postgres_host="host")
    dsn = mod._dsn()
    assert "my%20pass" in dsn
    assert "my+pass" not in dsn


# ---------------------------------------------------------------------------
# _get_psycopg lazy import
# ---------------------------------------------------------------------------


def test_get_psycopg_returns_none_when_not_installed(monkeypatch: pytest.MonkeyPatch):
    """_get_psycopg() returns None and sets flag when psycopg is not importable."""
    mod = _reload(monkeypatch)
    # Reset cached state so it retries
    mod._psycopg_module = None
    mod._psycopg_import_failed = False

    original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

    def _failing_import(name, *args, **kwargs):
        if name == "psycopg":
            raise ModuleNotFoundError("No module named 'psycopg'")
        return original_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=_failing_import):
        result = mod._get_psycopg()

    assert result is None
    assert mod._psycopg_import_failed is True


def test_get_psycopg_returns_cached_module(monkeypatch: pytest.MonkeyPatch):
    """_get_psycopg() returns the previously cached module without re-importing."""
    mod = _reload(monkeypatch)
    fake_psycopg = MagicMock()
    mod._psycopg_module = fake_psycopg
    mod._psycopg_import_failed = False
    result = mod._get_psycopg()
    assert result is fake_psycopg


def test_get_psycopg_returns_none_when_import_previously_failed(monkeypatch: pytest.MonkeyPatch):
    """_get_psycopg() skips import attempt when it already failed."""
    mod = _reload(monkeypatch)
    mod._psycopg_module = None
    mod._psycopg_import_failed = True
    result = mod._get_psycopg()
    assert result is None


# ---------------------------------------------------------------------------
# pg_* operations when Postgres is disabled
# ---------------------------------------------------------------------------


def test_pg_get_credential_returns_empty_when_disabled(monkeypatch: pytest.MonkeyPatch):
    """pg_get_credential() returns '' without connecting when POSTGRES_HOST is unset."""
    mod = _reload(monkeypatch, postgres_host="")
    assert mod.pg_get_credential("svc", "key") == ""


def test_pg_set_credential_raises_runtime_error_when_disabled(monkeypatch: pytest.MonkeyPatch):
    """pg_set_credential() raises RuntimeError when POSTGRES_HOST is not set."""
    mod = _reload(monkeypatch, postgres_host="")
    with pytest.raises(RuntimeError, match="POSTGRES_HOST is not set"):
        mod.pg_set_credential("svc", "key", "value")


def test_pg_set_credential_raises_runtime_error_when_psycopg_missing(
    monkeypatch: pytest.MonkeyPatch,
):
    """pg_set_credential() raises RuntimeError when psycopg is not installed."""
    mod = _reload(monkeypatch, postgres_host="localhost")
    mod._psycopg_module = None
    mod._psycopg_import_failed = True  # simulate missing psycopg
    with pytest.raises(RuntimeError, match="psycopg is not installed"):
        mod.pg_set_credential("svc", "key", "value")


# ---------------------------------------------------------------------------
# pg_get_credential_status: (value, store_reachable) from a single read
# ---------------------------------------------------------------------------


def _fake_conn(row):
    """A psycopg.connect(...) stand-in usable as ``with ... as conn, conn.cursor() as cur``."""
    cur = MagicMock()
    cur.fetchone.return_value = row
    cur.__enter__ = lambda s=cur: cur
    cur.__exit__ = lambda *a: False
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = lambda s=conn: conn
    conn.__exit__ = lambda *a: False
    return conn


def test_status_disabled_is_absent_but_reachable(monkeypatch: pytest.MonkeyPatch):
    # Disabled store is a configuration state ("absent"), not an outage.
    mod = _reload(monkeypatch, postgres_host="")
    assert mod.pg_get_credential_status("svc", "key") == ("", True)


def test_status_connection_error_is_unreachable(monkeypatch: pytest.MonkeyPatch):
    mod = _reload(monkeypatch, postgres_host="host")
    import psycopg

    def _boom(*_a, **_k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(psycopg, "connect", _boom)
    assert mod.pg_get_credential_status("svc", "key") == ("", False)


def test_status_missing_row_is_absent_reachable(monkeypatch: pytest.MonkeyPatch):
    mod = _reload(monkeypatch, postgres_host="host")
    import psycopg

    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: _fake_conn(None))
    assert mod.pg_get_credential_status("svc", "key") == ("", True)


def test_status_present_row_returns_decrypted_value(monkeypatch: pytest.MonkeyPatch):
    mod = _reload(monkeypatch, postgres_host="host")
    import psycopg

    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: _fake_conn(("ciphertext",)))
    fernet = MagicMock()
    fernet.decrypt.return_value = b"plain-secret"
    monkeypatch.setattr(mod, "get_integration_fernet", lambda: fernet)
    assert mod.pg_get_credential_status("svc", "key") == ("plain-secret", True)
    # pg_get_credential is a thin wrapper over the same single read.
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: _fake_conn(("ciphertext",)))
    assert mod.pg_get_credential("svc", "key") == "plain-secret"


def test_status_undecryptable_row_is_absent_reachable(monkeypatch: pytest.MonkeyPatch):
    # A row that won't decrypt (e.g. rotated key) is store-reachable but unusable →
    # treated as absent (a 400 path for callers), not an outage.
    mod = _reload(monkeypatch, postgres_host="host")
    import psycopg

    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: _fake_conn(("bad",)))
    fernet = MagicMock()
    fernet.decrypt.side_effect = ValueError("invalid token")
    monkeypatch.setattr(mod, "get_integration_fernet", lambda: fernet)
    assert mod.pg_get_credential_status("svc", "key") == ("", True)


def test_pg_delete_credential_noop_when_disabled(monkeypatch: pytest.MonkeyPatch):
    """pg_delete_credential() is a no-op and does not raise when POSTGRES_HOST is unset."""
    mod = _reload(monkeypatch, postgres_host="")
    mod.pg_delete_credential("svc", "key")  # must not raise


def test_pg_delete_service_credentials_noop_when_disabled(monkeypatch: pytest.MonkeyPatch):
    """pg_delete_service_credentials() is a no-op and does not raise when POSTGRES_HOST is unset."""
    mod = _reload(monkeypatch, postgres_host="")
    mod.pg_delete_service_credentials("svc")  # must not raise


def test_pg_delete_service_credentials_noop_when_psycopg_missing(
    monkeypatch: pytest.MonkeyPatch,
):
    """pg_delete_service_credentials() is a silent no-op when psycopg is missing."""
    mod = _reload(monkeypatch, postgres_host="localhost")
    mod._psycopg_module = None
    mod._psycopg_import_failed = True
    # Must not raise, must not try to open a connection.
    mod.pg_delete_service_credentials("svc")
