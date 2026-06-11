"""Encrypt the GitHub token the coding team persists for resume.

A GitHub-issue job must be resumable after its orchestrator thread dies (server restart, a
different worker process), and the standard deployment has no ``GITHUB_TOKEN`` env — the PAT
arrives per-request from the unified API credential store. So the token is persisted on the job
record. To keep a *usable* PAT out of the raw job record (which the generic
``GET /api/jobs/{team}`` route echoes verbatim), only **opaque Fernet ciphertext** is ever stored.

Key sourcing reuses the unified API's integration-credential key so no new secret is introduced:
``INTEGRATION_ENCRYPTION_KEY`` env first, else the ``integration.key`` file under the shared
``AGENT_CACHE`` volume (every team container mounts it). We never *generate* a key here — an
ephemeral key would make a token encrypted before a restart undecryptable afterwards, defeating the
whole point. When no key is available the token is simply not persisted and resume falls back to
``GITHUB_TOKEN`` env (or refuses), so we never store plaintext.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

_DEFAULT_CACHE_DIR = ".agent_cache"
_KEY_FILENAME = "integration.key"


def _load_key() -> Optional[bytes]:
    """Return the Fernet key (env or shared key file), or None when none is available.

    Postconditions:
        - Never generates or persists a key; a missing key yields None so callers fall back to
          not persisting the token rather than minting an ephemeral, restart-unstable key.
    """
    env_key = os.getenv("INTEGRATION_ENCRYPTION_KEY", "").strip()
    if env_key:
        return env_key.encode()
    cache_dir = os.getenv("AGENT_CACHE", _DEFAULT_CACHE_DIR)
    key_path = Path(cache_dir) / _KEY_FILENAME
    try:
        if key_path.exists():
            return key_path.read_bytes().strip()
    except OSError:
        logger.warning("Could not read encryption key at %s", key_path, exc_info=True)
    return None


def encrypt_token(token: str) -> Optional[str]:
    """Return Fernet ciphertext for ``token``.

    Postconditions:
        - Returns an opaque ciphertext string on success; returns None when ``token`` is empty,
          no key is available, or encryption fails — in which case the caller must not persist the
          token. Never raises.
    """
    if not token:
        return None
    key = _load_key()
    if not key:
        return None
    try:
        return Fernet(key).encrypt(token.encode()).decode()
    except Exception:
        logger.warning("GitHub token encryption failed; token will not be persisted.", exc_info=True)
        return None


def decrypt_token(ciphertext: Optional[str]) -> Optional[str]:
    """Return the plaintext token from ``ciphertext``.

    Postconditions:
        - Returns the decrypted token on success; returns None when ``ciphertext`` is falsy, no key
          is available, or decryption fails (wrong/rotated key, corruption). Never raises.
    """
    if not ciphertext:
        return None
    key = _load_key()
    if not key:
        return None
    try:
        return Fernet(key).decrypt(ciphertext.encode()).decode()
    except Exception:
        logger.warning("GitHub token decryption failed.", exc_info=True)
        return None
