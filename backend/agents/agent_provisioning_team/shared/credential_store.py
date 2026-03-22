"""
Secure credential storage using Fernet encryption.

Stores generated credentials encrypted at rest in .agent_cache/credentials/
"""

import json
import os
import secrets
import string
from pathlib import Path
from typing import Any, Dict, List, Optional

from cryptography.fernet import Fernet

DEFAULT_CREDENTIALS_DIR = Path(".agent_cache/provisioning_credentials")


class CredentialStore:
    """Secure credential storage with Fernet encryption."""

    def __init__(
        self,
        storage_dir: Optional[Path] = None,
        encryption_key: Optional[str] = None,
    ) -> None:
        self.storage_dir = storage_dir or DEFAULT_CREDENTIALS_DIR
        self.storage_dir.mkdir(parents=True, exist_ok=True)

        key = encryption_key or os.environ.get("PROVISION_CREDENTIAL_KEY") or None
        if key:
            key = key.strip() if isinstance(key, str) else key
        if not key:
            key = self._load_key_from_file() or self._load_or_generate_key()
        if isinstance(key, str):
            key = key.encode()
        try:
            self.fernet = Fernet(key)
        except ValueError:
            key = self._load_or_generate_key()
            self.fernet = Fernet(key)

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
                decrypted = self.fernet.decrypt(encrypted)
                existing = json.loads(decrypted.decode())
            except Exception:
                existing = {}
        
        existing[tool_name] = credentials
        
        encrypted = self.fernet.encrypt(json.dumps(existing).encode())
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
            decrypted = self.fernet.decrypt(encrypted)
            all_creds = json.loads(decrypted.decode())
            
            if tool_name:
                return all_creds.get(tool_name)
            return all_creds
        except Exception:
            return None

    def delete_credentials(self, agent_id: str) -> bool:
        """Delete all credentials for an agent."""
        path = self._agent_file(agent_id)
        if path.exists():
            path.unlink()
            return True
        return False

    def list_agents(self) -> List[str]:
        """List all agent IDs with stored credentials."""
        return [
            f.stem
            for f in self.storage_dir.glob("*.enc")
            if f.is_file()
        ]
