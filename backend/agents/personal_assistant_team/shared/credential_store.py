"""Secure credential storage with Fernet encryption."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class CredentialStoreError(Exception):
    """Raised when credential operations fail."""


class OAuthCredentials(BaseModel):
    """OAuth2 credentials with refresh capability."""

    provider: str
    access_token: str
    refresh_token: Optional[str] = None
    token_type: str = "Bearer"
    expires_at: Optional[str] = None
    scope: Optional[str] = None


class IMAPCredentials(BaseModel):
    """IMAP/SMTP credentials."""

    provider: str = "imap"
    host: str
    port: int = 993
    username: str
    password: str
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    use_ssl: bool = True


class StoredCredentials(BaseModel):
    """Container for all credentials for a user."""

    user_id: str
    email_credentials: Optional[Dict[str, Any]] = None
    calendar_credentials: Optional[Dict[str, Any]] = None
    other_credentials: Dict[str, Dict[str, Any]] = {}
    updated_at: str = ""


class CredentialStore:
    """
    Secure credential storage using Fernet symmetric encryption.
    
    Credentials are stored encrypted at rest in the file system.
    The encryption key must be provided via the PA_CREDENTIAL_KEY environment variable.
    """

    def __init__(
        self,
        storage_dir: Optional[str] = None,
        encryption_key: Optional[str] = None,
    ) -> None:
        """
        Initialize the credential store.
        
        Args:
            storage_dir: Directory for storing encrypted credentials.
                        Defaults to .agent_cache/credentials/
            encryption_key: Fernet encryption key. Defaults to PA_CREDENTIAL_KEY env var.
        """
        self.storage_dir = Path(
            storage_dir or os.getenv("PA_CREDENTIAL_DIR", ".agent_cache/credentials")
        )
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        
        key = encryption_key or os.getenv("PA_CREDENTIAL_KEY")
        if not key:
            logger.warning(
                "PA_CREDENTIAL_KEY not set. Generating temporary key. "
                "Credentials will not persist across restarts!"
            )
            key = Fernet.generate_key().decode()
        
        try:
            if isinstance(key, str):
                key = key.encode()
            self.fernet = Fernet(key)
        except Exception as e:
            raise CredentialStoreError(f"Invalid encryption key: {e}") from e

    def _get_user_file(self, user_id: str) -> Path:
        """Get the credential file path for a user."""
        user_dir = self.storage_dir / user_id
        user_dir.mkdir(parents=True, exist_ok=True)
        return user_dir / "credentials.enc"

    def _encrypt(self, data: Dict[str, Any]) -> bytes:
        """Encrypt data using Fernet."""
        json_data = json.dumps(data).encode()
        return self.fernet.encrypt(json_data)

    def _decrypt(self, encrypted_data: bytes) -> Dict[str, Any]:
        """Decrypt data using Fernet."""
        try:
            decrypted = self.fernet.decrypt(encrypted_data)
            return json.loads(decrypted.decode())
        except InvalidToken as e:
            raise CredentialStoreError("Failed to decrypt credentials. Key may have changed.") from e

    def _load_credentials(self, user_id: str) -> StoredCredentials:
        """Load credentials for a user."""
        file_path = self._get_user_file(user_id)
        
        if not file_path.exists():
            return StoredCredentials(user_id=user_id)
        
        try:
            encrypted_data = file_path.read_bytes()
            data = self._decrypt(encrypted_data)
            return StoredCredentials(**data)
        except Exception as e:
            logger.error("Failed to load credentials for user %s: %s", user_id, e)
            return StoredCredentials(user_id=user_id)

    def _save_credentials(self, credentials: StoredCredentials) -> None:
        """Save credentials for a user."""
        credentials.updated_at = datetime.utcnow().isoformat()
        file_path = self._get_user_file(credentials.user_id)
        
        try:
            encrypted_data = self._encrypt(credentials.model_dump())
            file_path.write_bytes(encrypted_data)
            logger.info("Saved credentials for user %s", credentials.user_id)
        except Exception as e:
            raise CredentialStoreError(f"Failed to save credentials: {e}") from e

    def store_email_credentials(
        self,
        user_id: str,
        credentials: OAuthCredentials | IMAPCredentials | Dict[str, Any],
    ) -> None:
        """
        Store email credentials for a user.
        
        Args:
            user_id: The user's ID
            credentials: OAuth or IMAP credentials
        """
        stored = self._load_credentials(user_id)
        
        if isinstance(credentials, (OAuthCredentials, IMAPCredentials)):
            stored.email_credentials = credentials.model_dump()
        else:
            stored.email_credentials = credentials
        
        self._save_credentials(stored)

    def get_email_credentials(self, user_id: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve email credentials for a user.
        
        Args:
            user_id: The user's ID
            
        Returns:
            Email credentials dict or None if not found
        """
        stored = self._load_credentials(user_id)
        return stored.email_credentials

    def store_calendar_credentials(
        self,
        user_id: str,
        credentials: OAuthCredentials | Dict[str, Any],
    ) -> None:
        """Store calendar API credentials for a user."""
        stored = self._load_credentials(user_id)
        
        if isinstance(credentials, OAuthCredentials):
            stored.calendar_credentials = credentials.model_dump()
        else:
            stored.calendar_credentials = credentials
        
        self._save_credentials(stored)

    def get_calendar_credentials(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve calendar credentials for a user."""
        stored = self._load_credentials(user_id)
        return stored.calendar_credentials

    def store_credentials(
        self,
        user_id: str,
        service_name: str,
        credentials: Dict[str, Any],
    ) -> None:
        """
        Store arbitrary credentials for a service.
        
        Args:
            user_id: The user's ID
            service_name: Name of the service (e.g., "openai", "twilio")
            credentials: Credentials dict
        """
        stored = self._load_credentials(user_id)
        stored.other_credentials[service_name] = credentials
        self._save_credentials(stored)

    def get_credentials(self, user_id: str, service_name: str) -> Optional[Dict[str, Any]]:
        """Retrieve credentials for a specific service."""
        stored = self._load_credentials(user_id)
        return stored.other_credentials.get(service_name)

    def delete_credentials(self, user_id: str, service_name: Optional[str] = None) -> bool:
        """
        Delete credentials.
        
        Args:
            user_id: The user's ID
            service_name: Specific service to delete, or None to delete all
            
        Returns:
            True if deletion was successful
        """
        stored = self._load_credentials(user_id)
        
        if service_name is None:
            file_path = self._get_user_file(user_id)
            if file_path.exists():
                file_path.unlink()
                logger.info("Deleted all credentials for user %s", user_id)
                return True
            return False
        
        if service_name == "email":
            stored.email_credentials = None
        elif service_name == "calendar":
            stored.calendar_credentials = None
        elif service_name in stored.other_credentials:
            del stored.other_credentials[service_name]
        else:
            return False
        
        self._save_credentials(stored)
        return True

    def has_email_credentials(self, user_id: str) -> bool:
        """Check if user has email credentials stored."""
        return self.get_email_credentials(user_id) is not None

    def has_calendar_credentials(self, user_id: str) -> bool:
        """Check if user has calendar credentials stored."""
        return self.get_calendar_credentials(user_id) is not None

    @staticmethod
    def generate_key() -> str:
        """Generate a new Fernet encryption key."""
        return Fernet.generate_key().decode()
