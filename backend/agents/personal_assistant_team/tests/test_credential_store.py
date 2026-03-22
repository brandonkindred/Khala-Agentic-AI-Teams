"""Tests for CredentialStore."""

import shutil
import tempfile

import pytest

from ..shared.credential_store import (
    CredentialStore,
    IMAPCredentials,
    OAuthCredentials,
)


class TestCredentialStore:
    """Tests for CredentialStore."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for testing."""
        temp = tempfile.mkdtemp()
        yield temp
        shutil.rmtree(temp)

    @pytest.fixture
    def store(self, temp_dir):
        """Create a CredentialStore with a temp directory and key."""
        key = CredentialStore.generate_key()
        return CredentialStore(storage_dir=temp_dir, encryption_key=key)

    def test_generate_key(self):
        """Test generating an encryption key."""
        key = CredentialStore.generate_key()
        
        assert key is not None
        assert len(key) > 0
        assert isinstance(key, str)

    def test_store_email_credentials_oauth(self, store):
        """Test storing OAuth email credentials."""
        creds = OAuthCredentials(
            provider="gmail",
            access_token="test_access_token",
            refresh_token="test_refresh_token",
        )
        
        store.store_email_credentials("test_user", creds)
        
        retrieved = store.get_email_credentials("test_user")
        
        assert retrieved is not None
        assert retrieved["access_token"] == "test_access_token"
        assert retrieved["refresh_token"] == "test_refresh_token"

    def test_store_email_credentials_imap(self, store):
        """Test storing IMAP email credentials."""
        creds = IMAPCredentials(
            host="imap.example.com",
            port=993,
            username="user@example.com",
            password="secret_password",
            smtp_host="smtp.example.com",
            smtp_port=587,
        )
        
        store.store_email_credentials("test_user", creds)
        
        retrieved = store.get_email_credentials("test_user")
        
        assert retrieved is not None
        assert retrieved["host"] == "imap.example.com"
        assert retrieved["password"] == "secret_password"

    def test_store_calendar_credentials(self, store):
        """Test storing calendar credentials."""
        creds = OAuthCredentials(
            provider="google",
            access_token="calendar_token",
        )
        
        store.store_calendar_credentials("test_user", creds)
        
        retrieved = store.get_calendar_credentials("test_user")
        
        assert retrieved is not None
        assert retrieved["access_token"] == "calendar_token"

    def test_store_generic_credentials(self, store):
        """Test storing generic service credentials."""
        creds = {"api_key": "secret_api_key", "endpoint": "https://api.example.com"}
        
        store.store_credentials("test_user", "custom_service", creds)
        
        retrieved = store.get_credentials("test_user", "custom_service")
        
        assert retrieved is not None
        assert retrieved["api_key"] == "secret_api_key"

    def test_get_nonexistent_credentials(self, store):
        """Test getting credentials that don't exist."""
        retrieved = store.get_email_credentials("nonexistent_user")
        
        assert retrieved is None

    def test_has_email_credentials(self, store):
        """Test checking if email credentials exist."""
        assert not store.has_email_credentials("test_user")
        
        store.store_email_credentials("test_user", {"provider": "test", "access_token": "token"})
        
        assert store.has_email_credentials("test_user")

    def test_delete_specific_credentials(self, store):
        """Test deleting specific credentials."""
        store.store_email_credentials("test_user", {"provider": "test", "access_token": "token"})
        store.store_calendar_credentials("test_user", {"provider": "google", "access_token": "cal_token"})
        
        result = store.delete_credentials("test_user", "email")
        
        assert result is True
        assert store.get_email_credentials("test_user") is None
        assert store.get_calendar_credentials("test_user") is not None

    def test_delete_all_credentials(self, store):
        """Test deleting all credentials for a user."""
        store.store_email_credentials("test_user", {"provider": "test", "access_token": "token"})
        store.store_calendar_credentials("test_user", {"provider": "google", "access_token": "cal_token"})
        
        result = store.delete_credentials("test_user")
        
        assert result is True
        assert store.get_email_credentials("test_user") is None
        assert store.get_calendar_credentials("test_user") is None

    def test_credentials_persistence(self, temp_dir):
        """Test that credentials persist across store instances."""
        key = CredentialStore.generate_key()
        
        store1 = CredentialStore(storage_dir=temp_dir, encryption_key=key)
        store1.store_email_credentials("test_user", {"provider": "test", "access_token": "token"})
        
        store2 = CredentialStore(storage_dir=temp_dir, encryption_key=key)
        retrieved = store2.get_email_credentials("test_user")
        
        assert retrieved is not None
        assert retrieved["access_token"] == "token"

    def test_wrong_key_fails(self, temp_dir):
        """Test that wrong key fails to decrypt."""
        key1 = CredentialStore.generate_key()
        key2 = CredentialStore.generate_key()
        
        store1 = CredentialStore(storage_dir=temp_dir, encryption_key=key1)
        store1.store_email_credentials("test_user", {"provider": "test", "access_token": "token"})
        
        store2 = CredentialStore(storage_dir=temp_dir, encryption_key=key2)
        retrieved = store2.get_email_credentials("test_user")
        
        assert retrieved is None
