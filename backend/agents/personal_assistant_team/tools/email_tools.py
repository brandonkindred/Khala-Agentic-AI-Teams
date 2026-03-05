"""Email tools for IMAP/SMTP and OAuth operations."""

from __future__ import annotations

import email
import imaplib
import logging
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

from ..models import EmailDraft, EmailMessage, EmailProvider
from ..shared.credential_store import CredentialStore, IMAPCredentials, OAuthCredentials

logger = logging.getLogger(__name__)


class EmailToolError(Exception):
    """Raised when email operations fail."""


class EmailToolAgent:
    """
    Tool agent for email operations.
    
    Supports:
    - IMAP/SMTP for generic email providers
    - OAuth2 for Gmail and Outlook (requires additional setup)
    """

    DEFAULT_IMAP_SETTINGS = {
        "gmail": {
            "host": "imap.gmail.com",
            "port": 993,
            "smtp_host": "smtp.gmail.com",
            "smtp_port": 587,
        },
        "outlook": {
            "host": "outlook.office365.com",
            "port": 993,
            "smtp_host": "smtp.office365.com",
            "smtp_port": 587,
        },
    }

    def __init__(self, credential_store: Optional[CredentialStore] = None) -> None:
        """Initialize the email tool agent."""
        self.credential_store = credential_store or CredentialStore()
        self._imap_connection: Optional[imaplib.IMAP4_SSL] = None
        self._current_user: Optional[str] = None

    def connect_imap(
        self,
        user_id: str,
        credentials: Optional[IMAPCredentials] = None,
    ) -> bool:
        """
        Connect to an IMAP server.
        
        Args:
            user_id: The user ID
            credentials: IMAP credentials (loads from store if not provided)
            
        Returns:
            True if connection successful
        """
        if credentials is None:
            cred_data = self.credential_store.get_email_credentials(user_id)
            if not cred_data:
                raise EmailToolError("No email credentials found for user")
            
            if cred_data.get("provider") == "oauth":
                return self._connect_oauth(user_id, cred_data)
            
            credentials = IMAPCredentials(**cred_data)
        
        try:
            self._imap_connection = imaplib.IMAP4_SSL(
                credentials.host,
                credentials.port,
            )
            self._imap_connection.login(credentials.username, credentials.password)
            self._current_user = user_id
            logger.info("Connected to IMAP server for user %s", user_id)
            return True
        except Exception as e:
            logger.error("Failed to connect to IMAP: %s", e)
            raise EmailToolError(f"IMAP connection failed: {e}") from e

    def _connect_oauth(self, user_id: str, cred_data: Dict[str, Any]) -> bool:
        """Connect using OAuth credentials (Gmail/Outlook)."""
        provider = cred_data.get("provider_type", "gmail")
        access_token = cred_data.get("access_token")
        
        if not access_token:
            raise EmailToolError("No access token in OAuth credentials")
        
        settings = self.DEFAULT_IMAP_SETTINGS.get(provider, self.DEFAULT_IMAP_SETTINGS["gmail"])
        
        try:
            self._imap_connection = imaplib.IMAP4_SSL(
                settings["host"],
                settings["port"],
            )
            
            email_addr = cred_data.get("email", cred_data.get("username", ""))
            auth_string = f"user={email_addr}\x01auth=Bearer {access_token}\x01\x01"
            self._imap_connection.authenticate("XOAUTH2", lambda x: auth_string.encode())
            
            self._current_user = user_id
            logger.info("Connected to %s via OAuth for user %s", provider, user_id)
            return True
        except Exception as e:
            logger.error("OAuth connection failed: %s", e)
            raise EmailToolError(f"OAuth connection failed: {e}") from e

    def disconnect(self) -> None:
        """Disconnect from the IMAP server."""
        if self._imap_connection:
            try:
                self._imap_connection.logout()
            except Exception:
                pass
            self._imap_connection = None
            self._current_user = None

    def fetch_inbox(
        self,
        user_id: str,
        limit: int = 20,
        folder: str = "INBOX",
        unread_only: bool = False,
    ) -> List[EmailMessage]:
        """
        Fetch emails from the inbox.
        
        Args:
            user_id: The user ID
            limit: Maximum number of emails to fetch
            folder: Folder to fetch from
            unread_only: Only fetch unread emails
            
        Returns:
            List of EmailMessage objects
        """
        if self._current_user != user_id:
            self.connect_imap(user_id)
        
        if not self._imap_connection:
            raise EmailToolError("Not connected to IMAP server")
        
        try:
            self._imap_connection.select(folder)
            
            search_criteria = "UNSEEN" if unread_only else "ALL"
            _, message_ids = self._imap_connection.search(None, search_criteria)
            
            ids = message_ids[0].split()
            ids = ids[-limit:] if len(ids) > limit else ids
            ids.reverse()
            
            messages = []
            for msg_id in ids:
                _, msg_data = self._imap_connection.fetch(msg_id, "(RFC822)")
                
                if msg_data[0] is None:
                    continue
                
                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)
                
                body = ""
                html_body = None
                
                if msg.is_multipart():
                    for part in msg.walk():
                        content_type = part.get_content_type()
                        if content_type == "text/plain":
                            body = part.get_payload(decode=True).decode(errors="replace")
                        elif content_type == "text/html":
                            html_body = part.get_payload(decode=True).decode(errors="replace")
                else:
                    body = msg.get_payload(decode=True).decode(errors="replace")
                
                messages.append(EmailMessage(
                    message_id=msg_id.decode(),
                    subject=msg.get("Subject", ""),
                    sender=msg.get("From", ""),
                    recipients=msg.get("To", "").split(","),
                    cc=msg.get("Cc", "").split(",") if msg.get("Cc") else [],
                    body=body,
                    html_body=html_body,
                    timestamp=msg.get("Date", ""),
                    is_read="\\Seen" in str(msg_data),
                ))
            
            return messages
        except Exception as e:
            logger.error("Failed to fetch emails: %s", e)
            raise EmailToolError(f"Failed to fetch emails: {e}") from e

    def send_email(
        self,
        user_id: str,
        draft: EmailDraft,
    ) -> bool:
        """
        Send an email.
        
        Args:
            user_id: The user ID
            draft: The email draft to send
            
        Returns:
            True if sent successfully
        """
        cred_data = self.credential_store.get_email_credentials(user_id)
        if not cred_data:
            raise EmailToolError("No email credentials found for user")
        
        if cred_data.get("provider") == "oauth":
            return self._send_oauth(user_id, draft, cred_data)
        
        credentials = IMAPCredentials(**cred_data)
        return self._send_smtp(credentials, draft)

    def _send_smtp(self, credentials: IMAPCredentials, draft: EmailDraft) -> bool:
        """Send email via SMTP."""
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = draft.subject
            msg["From"] = credentials.username
            msg["To"] = ", ".join(draft.to)
            
            if draft.cc:
                msg["Cc"] = ", ".join(draft.cc)
            
            msg.attach(MIMEText(draft.body, "plain"))
            if draft.html_body:
                msg.attach(MIMEText(draft.html_body, "html"))
            
            smtp_host = credentials.smtp_host or credentials.host.replace("imap", "smtp")
            smtp_port = credentials.smtp_port
            
            with smtplib.SMTP(smtp_host, smtp_port) as server:
                server.starttls()
                server.login(credentials.username, credentials.password)
                
                recipients = draft.to + draft.cc + draft.bcc
                server.sendmail(credentials.username, recipients, msg.as_string())
            
            logger.info("Email sent successfully")
            return True
        except Exception as e:
            logger.error("Failed to send email: %s", e)
            raise EmailToolError(f"Failed to send email: {e}") from e

    def _send_oauth(
        self,
        user_id: str,
        draft: EmailDraft,
        cred_data: Dict[str, Any],
    ) -> bool:
        """Send email via OAuth (Gmail/Outlook API)."""
        provider = cred_data.get("provider_type", "gmail")
        
        if provider == "gmail":
            return self._send_gmail(draft, cred_data)
        else:
            smtp_creds = IMAPCredentials(
                host=self.DEFAULT_IMAP_SETTINGS[provider]["host"],
                port=self.DEFAULT_IMAP_SETTINGS[provider]["port"],
                username=cred_data.get("email", ""),
                password=cred_data.get("access_token", ""),
                smtp_host=self.DEFAULT_IMAP_SETTINGS[provider]["smtp_host"],
                smtp_port=self.DEFAULT_IMAP_SETTINGS[provider]["smtp_port"],
            )
            return self._send_smtp(smtp_creds, draft)

    def _send_gmail(self, draft: EmailDraft, cred_data: Dict[str, Any]) -> bool:
        """Send email via Gmail API."""
        try:
            from googleapiclient.discovery import build
            from google.oauth2.credentials import Credentials
        except ImportError:
            logger.warning("Google API client not installed, falling back to SMTP")
            return False
        
        try:
            creds = Credentials(
                token=cred_data.get("access_token"),
                refresh_token=cred_data.get("refresh_token"),
                token_uri="https://oauth2.googleapis.com/token",
                client_id=os.getenv("GOOGLE_CLIENT_ID"),
                client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
            )
            
            service = build("gmail", "v1", credentials=creds)
            
            msg = MIMEMultipart("alternative")
            msg["Subject"] = draft.subject
            msg["To"] = ", ".join(draft.to)
            if draft.cc:
                msg["Cc"] = ", ".join(draft.cc)
            
            msg.attach(MIMEText(draft.body, "plain"))
            if draft.html_body:
                msg.attach(MIMEText(draft.html_body, "html"))
            
            import base64
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            
            service.users().messages().send(
                userId="me",
                body={"raw": raw}
            ).execute()
            
            logger.info("Email sent via Gmail API")
            return True
        except Exception as e:
            logger.error("Failed to send via Gmail API: %s", e)
            raise EmailToolError(f"Gmail API error: {e}") from e

    def create_draft(
        self,
        user_id: str,
        draft: EmailDraft,
    ) -> str:
        """
        Create a draft email (Gmail only currently).
        
        Args:
            user_id: The user ID
            draft: The draft to create
            
        Returns:
            Draft ID
        """
        cred_data = self.credential_store.get_email_credentials(user_id)
        if not cred_data or cred_data.get("provider_type") != "gmail":
            raise EmailToolError("Draft creation only supported for Gmail")
        
        try:
            from googleapiclient.discovery import build
            from google.oauth2.credentials import Credentials
        except ImportError:
            raise EmailToolError("Google API client not installed")
        
        try:
            creds = Credentials(
                token=cred_data.get("access_token"),
                refresh_token=cred_data.get("refresh_token"),
                token_uri="https://oauth2.googleapis.com/token",
                client_id=os.getenv("GOOGLE_CLIENT_ID"),
                client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
            )
            
            service = build("gmail", "v1", credentials=creds)
            
            msg = MIMEMultipart("alternative")
            msg["Subject"] = draft.subject
            msg["To"] = ", ".join(draft.to)
            if draft.cc:
                msg["Cc"] = ", ".join(draft.cc)
            
            msg.attach(MIMEText(draft.body, "plain"))
            
            import base64
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            
            result = service.users().drafts().create(
                userId="me",
                body={"message": {"raw": raw}}
            ).execute()
            
            return result["id"]
        except Exception as e:
            logger.error("Failed to create draft: %s", e)
            raise EmailToolError(f"Failed to create draft: {e}") from e

    def search_emails(
        self,
        user_id: str,
        query: str,
        limit: int = 20,
    ) -> List[EmailMessage]:
        """
        Search emails using IMAP search.
        
        Args:
            user_id: The user ID
            query: Search query (IMAP format)
            limit: Maximum results
            
        Returns:
            List of matching emails
        """
        if self._current_user != user_id:
            self.connect_imap(user_id)
        
        if not self._imap_connection:
            raise EmailToolError("Not connected to IMAP server")
        
        try:
            self._imap_connection.select("INBOX")
            _, message_ids = self._imap_connection.search(None, query)
            
            ids = message_ids[0].split()[-limit:]
            ids.reverse()
            
            messages = []
            for msg_id in ids:
                _, msg_data = self._imap_connection.fetch(msg_id, "(RFC822)")
                
                if msg_data[0] is None:
                    continue
                
                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)
                
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True).decode(errors="replace")
                            break
                else:
                    body = msg.get_payload(decode=True).decode(errors="replace")
                
                messages.append(EmailMessage(
                    message_id=msg_id.decode(),
                    subject=msg.get("Subject", ""),
                    sender=msg.get("From", ""),
                    recipients=msg.get("To", "").split(","),
                    body=body,
                    timestamp=msg.get("Date", ""),
                ))
            
            return messages
        except Exception as e:
            logger.error("Search failed: %s", e)
            raise EmailToolError(f"Search failed: {e}") from e


def generate_oauth_url(provider: str = "gmail") -> str:
    """
    Generate OAuth authorization URL.
    
    Args:
        provider: Email provider (gmail or outlook)
        
    Returns:
        Authorization URL
    """
    client_id = os.getenv("GOOGLE_CLIENT_ID" if provider == "gmail" else "OUTLOOK_CLIENT_ID")
    
    if not client_id:
        raise EmailToolError(f"No client ID configured for {provider}")
    
    if provider == "gmail":
        return (
            "https://accounts.google.com/o/oauth2/v2/auth"
            f"?client_id={client_id}"
            "&redirect_uri=urn:ietf:wg:oauth:2.0:oob"
            "&response_type=code"
            "&scope=https://mail.google.com/"
            "&access_type=offline"
        )
    else:
        return (
            "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
            f"?client_id={client_id}"
            "&redirect_uri=http://localhost"
            "&response_type=code"
            "&scope=https://outlook.office.com/IMAP.AccessAsUser.All"
        )
