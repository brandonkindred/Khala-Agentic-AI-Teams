"""Tests for the resume-token encryption helper: round-trip, no-key fallback, bad input."""

from __future__ import annotations

from cryptography.fernet import Fernet

from coding_team import token_crypto


def _set_key(monkeypatch) -> str:
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("INTEGRATION_ENCRYPTION_KEY", key)
    return key


def test_round_trip(monkeypatch):
    _set_key(monkeypatch)
    ct = token_crypto.encrypt_token("ghp_secret")
    assert ct is not None
    assert ct != "ghp_secret"  # opaque ciphertext, not the plaintext
    assert token_crypto.decrypt_token(ct) == "ghp_secret"


def test_no_key_means_no_persistence(monkeypatch, tmp_path):
    monkeypatch.delenv("INTEGRATION_ENCRYPTION_KEY", raising=False)
    # Point AGENT_CACHE at an empty dir so no key file is found, and we never generate one.
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    assert token_crypto.encrypt_token("ghp_secret") is None
    assert token_crypto.decrypt_token("anything") is None


def test_reads_key_file_when_env_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("INTEGRATION_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    (tmp_path / "integration.key").write_bytes(Fernet.generate_key())
    ct = token_crypto.encrypt_token("ghp_secret")
    assert ct is not None
    assert token_crypto.decrypt_token(ct) == "ghp_secret"


def test_empty_and_none_inputs(monkeypatch):
    _set_key(monkeypatch)
    assert token_crypto.encrypt_token("") is None
    assert token_crypto.decrypt_token(None) is None
    assert token_crypto.decrypt_token("") is None


def test_decrypt_bad_ciphertext_returns_none(monkeypatch):
    _set_key(monkeypatch)
    assert token_crypto.decrypt_token("not-valid-fernet") is None


def test_decrypt_with_wrong_key_returns_none(monkeypatch):
    _set_key(monkeypatch)
    ct = token_crypto.encrypt_token("ghp_secret")
    # Rotate the key: the old ciphertext is no longer decryptable, but we fail closed, not raise.
    monkeypatch.setenv("INTEGRATION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    assert token_crypto.decrypt_token(ct) is None
