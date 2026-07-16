"""
Secure credential storage using Fernet encryption.

Stores generated credentials encrypted at rest under
``${AGENT_CACHE:-.agent_cache}/agent_provisioning/credentials/`` (the same
durable volume used by job/env state in compose).

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


def default_credentials_dir() -> Path:
    """Resolve the durable on-disk credential directory.

    Preconditions:
        * None.
    Postconditions:
        * Returns ``${AGENT_CACHE:-.agent_cache}/agent_provisioning/credentials``
          as a ``Path`` (directory need not exist yet).
    """
    root = Path(os.environ.get("AGENT_CACHE", ".agent_cache"))
    return root / "agent_provisioning" / "credentials"


def legacy_credentials_dirs() -> List[Path]:
    """Return pre-cutover credential directories for read/delete fallback.

    Preconditions:
        * None.
    Postconditions:
        * Includes the historical default ``.agent_cache/provisioning_credentials``
          and the same relative layout under ``AGENT_CACHE`` when set.
    """
    root = Path(os.environ.get("AGENT_CACHE", ".agent_cache"))
    return [
        Path(".agent_cache") / "provisioning_credentials",
        root / "provisioning_credentials",
    ]


# Retained for callers/tests that import the historical module constant.
DEFAULT_CREDENTIALS_DIR = default_credentials_dir()


class CredentialStoreConfigError(RuntimeError):
    """Raised when PROVISION_REQUIRE_KEY=1 is set but no valid key exists."""


class CredentialStore:
    """Secure credential storage with Fernet encryption + rotation support."""

    def __init__(
        self,
        storage_dir: Optional[Path] = None,
        encryption_key: Optional[str] = None,
    ) -> None:
        self.storage_dir = Path(storage_dir) if storage_dir is not None else default_credentials_dir()
        self.storage_dir.mkdir(parents=True, exist_ok=True)

        keys = self._collect_keys(encryption_key)
        if not keys:
            if os.environ.get("PROVISION_REQUIRE_KEY", "").lower() in ("1", "true", "yes"):
                raise CredentialStoreConfigError(
                    "PROVISION_REQUIRE_KEY is set but no valid credential key was "
                    "found. Set PROVISION_CREDENTIAL_KEY or PA_CREDENTIAL_KEY_FILE."
                )
            keys = [self._load_or_generate_key()]
        elif (
            encryption_key is None
            and not os.environ.get("PROVISION_CREDENTIAL_KEY", "").strip()
            and not os.environ.get("PA_CREDENTIAL_KEY_FILE")
            and self._read_valid_key(self.storage_dir / ".encryption_key") is None
        ):
            # Upgrade: we decrypted via a legacy ``.encryption_key`` — persist it
            # as the primary store key so later boots do not mint a divergent key.
            self._publish_key_file(self.storage_dir / ".encryption_key", keys[0])

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

        # Primary store generated key — include whenever present so a store that
        # already minted ``.encryption_key`` remains the encrypt preference, with
        # legacy keys appended below as trailing decrypt-only keys.
        if not override:
            primary_key = self._read_valid_key(self.storage_dir / ".encryption_key")
            if primary_key is not None:
                keys.append(primary_key)

        # Legacy cutover: include generated fallback keys from the old
        # ``provisioning_credentials`` directories as trailing decrypt keys so
        # MultiFernet can still open pre-move ``.enc`` files when the primary
        # store minted a fresh ``.encryption_key``.
        for legacy_key in self._legacy_fallback_keys():
            keys.append(legacy_key)

        # Dedup while preserving order — first key wins for encryption.
        seen = set()
        out: List[bytes] = []
        for k in keys:
            if k not in seen:
                seen.add(k)
                out.append(k)
        return out

    @staticmethod
    def _legacy_fallback_keys() -> List[bytes]:
        """Load valid ``.encryption_key`` files from pre-cutover credential dirs."""
        keys: List[bytes] = []
        for legacy_dir in legacy_credentials_dirs():
            key = CredentialStore._read_valid_key(legacy_dir / ".encryption_key")
            if key is not None:
                keys.append(key)
        return keys

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
        """Load the dev-fallback key, generating it once, race-safely.

        ``storage_dir`` can be shared by concurrent processes (e.g. parallel
        test workers, or multiple workers of the same service). Two correctness
        hazards are handled:

        * **Partial read.** The old check-then-``write_bytes`` sequence let one
          process observe the file a peer had just created but not finished
          writing, reading an empty/partial value and raising ``Fernet key must
          be 32 url-safe base64-encoded bytes``. Publication is now atomic — a
          fresh key is written to a unique temp file and ``os.replace``d into
          place, so a concurrent reader sees either the previous key or the
          complete new one, never a partial file.
        * **Clobbering a published key.** Two first-time processes could each
          pass the initial read, generate *different* keys, and both publish —
          the second replace would overwrite the first's key after that process
          had already encrypted credentials under it, making them permanently
          undecryptable. Generation is therefore serialized by an advisory lock
          and re-checks the key under the lock: whoever wins publishes; everyone
          else returns the published key and never overwrites it.

        A present-but-invalid file (empty/stale/partial) is treated as absent
        and repaired under the same lock (no valid key is ever overwritten).

        Postconditions:
            * Returns a syntactically valid url-safe base64 Fernet key.
            * No caller observes a partially-written key file.
            * A valid key already published by a concurrent process is never
              overwritten (on hosts where the advisory lock is honoured).
        """
        key_file = self.storage_dir / ".encryption_key"

        # Fast path: a valid key already exists — no lock needed.
        existing = self._read_valid_key(key_file)
        if existing is not None:
            return existing

        # Upgrade path: migrate a legacy generated key into the primary store
        # before minting a new one (otherwise legacy ``.enc`` files become
        # undecryptable under a fresh primary key).
        for legacy_key in self._legacy_fallback_keys():
            self._publish_key_file(key_file, legacy_key)
            return legacy_key

        # First-time creation (or repair of an invalid/partial file). Serialize
        # across processes sharing storage_dir so peers cannot each generate a
        # different key and clobber the other's.
        lock_file = self.storage_dir / ".encryption_key.lock"
        with open(lock_file, "w") as handle:
            self._lock_exclusive(handle)
            # Re-check under the lock: a peer may have published while we waited.
            existing = self._read_valid_key(key_file)
            if existing is not None:
                return existing

            key = Fernet.generate_key()
            self._publish_key_file(key_file, key)
            return key

    def _publish_key_file(self, key_file: Path, key: bytes) -> None:
        """Atomically write ``key`` to ``key_file`` (same-filesystem replace)."""
        key_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = key_file.parent / f".encryption_key.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        try:
            tmp.write_bytes(key)
            tmp.chmod(0o600)
            os.replace(tmp, key_file)
        finally:
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _lock_exclusive(handle) -> None:
        """Best-effort exclusive advisory lock on an open file ``handle``.

        Serializes first-time key creation across processes that share the
        storage dir on a single host. Degrades to a no-op where flock is
        unavailable (non-POSIX, or a filesystem that does not support it); the
        atomic ``os.replace`` publication still prevents partial reads there,
        leaving only the pre-existing dev-fallback write-write race.
        """
        try:
            import fcntl
        except ImportError:  # pragma: no cover - non-POSIX platform
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError:  # pragma: no cover - lock unsupported on this filesystem
            pass

    @staticmethod
    def _read_valid_key(key_file: Path) -> Optional[bytes]:
        """Return a syntactically valid Fernet key from ``key_file``, else None.

        Tolerates an absent file and a present-but-empty/partial file (the
        window where a concurrent writer has created but not finished writing
        the key), so callers can retry rather than constructing ``Fernet`` on a
        truncated value.
        """
        try:
            raw = key_file.read_bytes().strip()
        except FileNotFoundError:
            return None
        if not raw:
            return None
        try:
            Fernet(raw)
        except (ValueError, TypeError):
            return None
        return raw

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
        """Get the credentials file path for an agent in the primary store."""
        return self.storage_dir / f"{agent_id}.enc"

    def _agent_file_candidates(self, agent_id: str) -> List[Path]:
        """Primary path first, then legacy locations from before the AGENT_CACHE move."""
        return [self._agent_file(agent_id)] + [
            legacy / f"{agent_id}.enc" for legacy in legacy_credentials_dirs()
        ]

    def _read_agent_credentials(self, agent_id: str) -> tuple[Optional[Dict[str, Any]], Optional[Path]]:
        """Load decrypted credentials from the primary or a legacy path."""
        for path in self._agent_file_candidates(agent_id):
            if not path.exists():
                continue
            try:
                encrypted = path.read_bytes()
                decrypted = self.multifernet.decrypt(encrypted)
                return json.loads(decrypted.decode()), path
            except (InvalidToken, ValueError, OSError):
                continue
        return None, None

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
        all_creds, _src = self._read_agent_credentials(agent_id)
        if all_creds is None:
            return None
        if tool_name:
            return all_creds.get(tool_name)
        return all_creds

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
        """Delete all credentials for an agent (primary and legacy paths)."""
        deleted = False
        for path in self._agent_file_candidates(agent_id):
            if path.exists():
                path.unlink()
                deleted = True
        return deleted

    def list_agents(self) -> List[str]:
        """List all agent IDs with stored credentials."""
        seen: set[str] = set()
        for directory in [self.storage_dir, *legacy_credentials_dirs()]:
            if not directory.exists():
                continue
            for path in directory.glob("*.enc"):
                if path.is_file():
                    seen.add(path.stem)
        return sorted(seen)
