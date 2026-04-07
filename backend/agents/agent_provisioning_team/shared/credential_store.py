"""
Secure credential storage using Fernet encryption.

Stores generated credentials encrypted at rest in .agent_cache/credentials/

Key management
--------------
The store understands three sources of keys, in priority order:

1. ``encryption_key=`` constructor argument (tests / explicit wiring).
2. ``PROVISION_CREDENTIAL_KEY`` env var — comma-separated list of Fernet
   keys. The FIRST key is always used for new encryptions; trailing keys
   remain valid for decryption. This enables zero-downtime rotation via
   ``cryptography.fernet.MultiFernet``.
3. A key file at ``PA_CREDENTIAL_KEY_FILE`` or the dev fallback
   ``<storage_dir>/.encryption_key`` (auto-generated in dev).

In production set ``PROVISION_REQUIRE_KEY=1`` to disable the dev fallback
and hard-fail if no key is configured.
"""

import json
import os
import secrets
import string
from pathlib import Path
from typing import Any, Dict, List, Optional

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

DEFAULT_CREDENTIALS_DIR = Path(".agent_cache/provisioning_credentials")


class CredentialStoreConfigError(RuntimeError):
    """Raised when PROVISION_REQUIRE_KEY=1 is set but no valid key exists."""


class CredentialStore:
    """Secure credential storage with Fernet encryption + rotation support."""

    def __init__(
        self,
        storage_dir: Optional[Path] = None,
        encryption_key: Optional[str] = None,
    ) -> None:
        self.storage_dir = storage_dir or DEFAULT_CREDENTIALS_DIR
        self.storage_dir.mkdir(parents=True, exist_ok=True)

        keys = self._collect_keys(encryption_key)
        if not keys:
            if os.environ.get("PROVISION_REQUIRE_KEY", "").lower() in ("1", "true", "yes"):
                raise CredentialStoreConfigError(
                    "PROVISION_REQUIRE_KEY is set but no valid credential key was "
                    "found. Set PROVISION_CREDENTIAL_KEY or PA_CREDENTIAL_KEY_FILE."
                )
            keys = [self._load_or_generate_key()]

        try:
            self._fernets = [Fernet(k) for k in keys]
        except ValueError as e:
            raise CredentialStoreConfigError(f"Invalid credential key: {e}") from e

        self.multifernet = MultiFernet(self._fernets)
        # Back-compat attribute (old tests may still read .fernet).
        self.fernet = self._fernets[0]

    # ---- key loading ----
    def _collect_keys(self, override: Optional[str]) -> List[bytes]:
        """Collect keys from explicit arg / env / file, in priority order."""
        keys: List[bytes] = []

        if override:
            keys.extend(self._parse_key_list(override))

        env = os.environ.get("PROVISION_CREDENTIAL_KEY", "").strip()
        if env and not override:
            keys.extend(self._parse_key_list(env))

        file_key = self._load_key_from_file()
        if file_key:
            keys.append(file_key)

        # Dedup while preserving order — first key wins for encryption.
        seen = set()
        out: List[bytes] = []
        for k in keys:
            if k not in seen:
                seen.add(k)
                out.append(k)
        return out

    @staticmethod
    def _parse_key_list(raw: str) -> List[bytes]:
        """Parse a comma-separated list of Fernet keys, skipping blanks."""
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        return [p.encode() if isinstance(p, str) else p for p in parts]

    def _load_key_from_file(self) -> Optional[bytes]:
        """Load key from PA_CREDENTIAL_KEY_FILE if set (e.g. Docker build-time key)."""
        key_file_path = os.environ.get("PA_CREDENTIAL_KEY_FILE")
        if not key_file_path:
            return None
        path = Path(key_file_path)
        if not path.exists():
            return None
        raw = path.read_bytes()
        return raw.strip()

    def _load_or_generate_key(self) -> bytes:
        """Load existing key or generate a new one."""
        key_file = self.storage_dir / ".encryption_key"

        if key_file.exists():
            raw = key_file.read_bytes()
            return raw.strip()

        key = Fernet.generate_key()
        key_file.write_bytes(key)
        key_file.chmod(0o600)
        return key

    @staticmethod
    def generate_key() -> str:
        """Generate a new Fernet encryption key."""
        return Fernet.generate_key().decode()

    @staticmethod
    def generate_password(length: int = 32) -> str:
        """Generate a cryptographically secure password."""
        alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
        return "".join(secrets.choice(alphabet) for _ in range(length))

    @staticmethod
    def generate_token(length: int = 64) -> str:
        """Generate a cryptographically secure token."""
        return secrets.token_urlsafe(length)

    @staticmethod
    def generate_username(agent_id: str, tool_name: str) -> str:
        """Generate a username from agent ID and tool name."""
        safe_agent_id = "".join(c if c.isalnum() else "_" for c in agent_id)
        safe_tool = "".join(c if c.isalnum() else "_" for c in tool_name)
        return f"agent_{safe_agent_id}_{safe_tool}"[:63]

    def _agent_file(self, agent_id: str) -> Path:
        """Get the credentials file path for an agent."""
        return self.storage_dir / f"{agent_id}.enc"

    def store_credentials(
        self,
        agent_id: str,
        tool_name: str,
        credentials: Dict[str, Any],
    ) -> None:
        """Store credentials for a tool, encrypted at rest."""
        path = self._agent_file(agent_id)

        existing: Dict[str, Dict[str, Any]] = {}
        if path.exists():
            try:
                encrypted = path.read_bytes()
                decrypted = self.multifernet.decrypt(encrypted)
                existing = json.loads(decrypted.decode())
            except (InvalidToken, ValueError, OSError):
                existing = {}

        existing[tool_name] = credentials

        encrypted = self.multifernet.encrypt(json.dumps(existing).encode())
        path.write_bytes(encrypted)
        path.chmod(0o600)

    def get_credentials(
        self,
        agent_id: str,
        tool_name: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Retrieve credentials for an agent (all or specific tool)."""
        path = self._agent_file(agent_id)

        if not path.exists():
            return None

        try:
            encrypted = path.read_bytes()
            decrypted = self.multifernet.decrypt(encrypted)
            all_creds = json.loads(decrypted.decode())

            if tool_name:
                return all_creds.get(tool_name)
            return all_creds
        except (InvalidToken, ValueError, OSError):
            return None

    def rotate_key(self, new_key: str) -> int:
        """Re-encrypt every stored agent file with a new Fernet key.

        ``new_key`` is prepended to the key list and becomes the active
        encryption key. Trailing (old) keys remain valid until callers
        remove them from ``PROVISION_CREDENTIAL_KEY``. Returns the number
        of files re-encrypted.

        Callers are responsible for persisting ``new_key`` to their
        secret manager / env var *before* calling this method; otherwise
        a restart will lose the new key.
        """
        new_key_bytes = new_key.encode() if isinstance(new_key, str) else new_key
        try:
            new_fernet = Fernet(new_key_bytes)
        except ValueError as e:
            raise CredentialStoreConfigError(f"Invalid new key: {e}") from e

        # Prepend new key so it becomes the active encryption key.
        self._fernets = [new_fernet, *self._fernets]
        self.multifernet = MultiFernet(self._fernets)
        self.fernet = new_fernet

        rotated = 0
        for path in self.storage_dir.glob("*.enc"):
            try:
                encrypted = path.read_bytes()
                re_encrypted = self.multifernet.rotate(encrypted)
                path.write_bytes(re_encrypted)
                path.chmod(0o600)
                rotated += 1
            except (InvalidToken, ValueError, OSError):
                # Skip unreadable files; don't abort the rotation.
                continue
        return rotated

    def delete_credentials(self, agent_id: str) -> bool:
        """Delete all credentials for an agent."""
        path = self._agent_file(agent_id)
        if path.exists():
            path.unlink()
            return True
        return False

    def list_agents(self) -> List[str]:
        """List all agent IDs with stored credentials."""
        return [f.stem for f in self.storage_dir.glob("*.enc") if f.is_file()]
