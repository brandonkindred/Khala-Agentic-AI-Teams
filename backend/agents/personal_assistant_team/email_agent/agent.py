"""Email Agent - manages email reading, drafting, and event extraction."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..models import EmailDraft, EmailMessage
from ..shared.credential_store import CredentialStore, IMAPCredentials, OAuthCredentials
from ..shared.llm import JSONExtractionFailure, LLMClient
from ..shared.user_profile_store import UserProfileStore
from ..tools.email_tools import EmailToolAgent
from .models import (
    ConnectEmailRequest,
    DraftResult,
    EmailDraftRequest,
    EmailReadRequest,
    EmailSearchRequest,
    EmailSendRequest,
    EmailSummary,
)
from .prompts import (
    EMAIL_DRAFT_PROMPT,
    EMAIL_SUMMARY_PROMPT,
    EVENT_EXTRACTION_PROMPT,
    SMART_REPLY_PROMPT,
)

logger = logging.getLogger(__name__)


class EmailAgent:
    """
    Agent for managing email operations.

    Capabilities:
    - Read and summarize emails
    - Draft emails matching user's voice
    - Extract events from emails
    - Manage email credentials
    """

    def __init__(
        self,
        llm: LLMClient,
        credential_store: Optional[CredentialStore] = None,
        profile_store: Optional[UserProfileStore] = None,
    ) -> None:
        """
        Initialize the Email Agent.

        Args:
            llm: LLM client for text generation
            credential_store: Credential storage
            profile_store: User profile storage
        """
        self.llm = llm
        self.credential_store = credential_store or CredentialStore()
        self.profile_store = profile_store or UserProfileStore()
        self.email_tool = EmailToolAgent(self.credential_store)

    def connect_email(self, request: ConnectEmailRequest) -> bool:
        """
        Connect an email account.

        Args:
            request: Connection request with credentials

        Returns:
            True if connection successful
        """
        provider = request.provider.lower()

        if provider in ("gmail", "outlook"):
            creds = OAuthCredentials(
                provider=provider,
                access_token=request.credentials.get("access_token", ""),
                refresh_token=request.credentials.get("refresh_token"),
                expires_at=request.credentials.get("expires_at"),
            )
            self.credential_store.store_email_credentials(request.user_id, creds)
        else:
            creds = IMAPCredentials(
                host=request.credentials.get("host", ""),
                port=request.credentials.get("port", 993),
                username=request.credentials.get("username", ""),
                password=request.credentials.get("password", ""),
                smtp_host=request.credentials.get("smtp_host"),
                smtp_port=request.credentials.get("smtp_port", 587),
            )
            self.credential_store.store_email_credentials(request.user_id, creds)

        return self.email_tool.connect_imap(request.user_id)

    def has_credentials(self, user_id: str) -> bool:
        """Check if user has email credentials."""
        return self.credential_store.has_email_credentials(user_id)

    def read_emails(self, request: EmailReadRequest) -> List[EmailMessage]:
        """
        Read emails from inbox.

        Args:
            request: Read request parameters

        Returns:
            List of email messages
        """
        return self.email_tool.fetch_inbox(
            user_id=request.user_id,
            limit=request.limit,
            folder=request.folder,
            unread_only=request.unread_only,
        )

    def summarize_email(self, email: EmailMessage) -> EmailSummary:
        """
        Generate a summary of an email.

        Args:
            email: The email to summarize

        Returns:
            EmailSummary with key information
        """
        prompt = EMAIL_SUMMARY_PROMPT.format(
            sender=email.sender,
            subject=email.subject,
            date=email.timestamp,
            body=email.body[:8000],
        )

        try:
            data = self.llm.complete_json(
                prompt,
                temperature=0.2,
                expected_keys=[
                    "summary",
                    "key_points",
                    "extracted_events",
                    "action_items",
                    "sentiment",
                ],
            )
        except JSONExtractionFailure as e:
            logger.error("Failed to summarize email (JSON extraction failed):\n%s", e)
            return EmailSummary(
                message_id=email.message_id,
                subject=email.subject,
                sender=email.sender,
                summary="Failed to generate summary - JSON extraction error. See logs for details.",
            )
        except Exception as e:
            logger.error("Failed to summarize email: %s", e)
            return EmailSummary(
                message_id=email.message_id,
                subject=email.subject,
                sender=email.sender,
                summary="Failed to generate summary",
            )

        return EmailSummary(
            message_id=email.message_id,
            subject=email.subject,
            sender=email.sender,
            summary=data.get("summary", ""),
            key_points=data.get("key_points", []),
            extracted_events=data.get("extracted_events", []),
            action_items=data.get("action_items", []),
            sentiment=data.get("sentiment", "neutral"),
        )

    def summarize_inbox(
        self,
        user_id: str,
        limit: int = 10,
    ) -> List[EmailSummary]:
        """
        Summarize recent emails in inbox.

        Args:
            user_id: The user ID
            limit: Number of emails to summarize

        Returns:
            List of email summaries
        """
        emails = self.read_emails(
            EmailReadRequest(
                user_id=user_id,
                limit=limit,
                unread_only=False,
            )
        )

        summaries = []
        for email in emails:
            summary = self.summarize_email(email)
            summaries.append(summary)

        return summaries

    def extract_events(self, email: EmailMessage) -> List[Dict[str, Any]]:
        """
        Extract calendar events from an email.

        Args:
            email: The email to analyze

        Returns:
            List of extracted events
        """
        prompt = EVENT_EXTRACTION_PROMPT.format(
            sender=email.sender,
            subject=email.subject,
            date=email.timestamp,
            body=email.body[:8000],
        )

        try:
            data = self.llm.complete_json(prompt, temperature=0.1)
            return data.get("events", [])
        except Exception as e:
            logger.error("Failed to extract events: %s", e)
            return []

    def extract_events_from_inbox(
        self,
        user_id: str,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Extract events from recent emails.

        Args:
            user_id: The user ID
            limit: Number of emails to process

        Returns:
            List of all extracted events
        """
        emails = self.read_emails(
            EmailReadRequest(
                user_id=user_id,
                limit=limit,
            )
        )

        all_events = []
        for email in emails:
            events = self.extract_events(email)
            for event in events:
                event["source_email"] = {
                    "message_id": email.message_id,
                    "subject": email.subject,
                    "sender": email.sender,
                }
                all_events.append(event)

        return all_events

    def draft_email(self, request: EmailDraftRequest) -> DraftResult:
        """
        Draft an email based on user intent.

        Args:
            request: Draft request with intent and context

        Returns:
            DraftResult with generated email
        """
        profile_summary = self.profile_store.get_profile_summary(request.user_id)

        writing_style = "Professional and friendly"

        reply_context = ""
        if request.reply_to_message_id:
            reply_context = f"This is a reply to message ID: {request.reply_to_message_id}"

        prompt = EMAIL_DRAFT_PROMPT.format(
            profile_summary=profile_summary or "No profile available",
            writing_style=writing_style,
            intent=request.intent,
            context=request.context,
            reply_context=reply_context,
        )

        try:
            data = self.llm.complete_json(prompt, temperature=0.4)
        except Exception as e:
            logger.error("Failed to draft email: %s", e)
            return DraftResult(
                subject="",
                body="Failed to generate draft",
            )

        return DraftResult(
            subject=data.get("subject", ""),
            body=data.get("body", ""),
            suggested_recipients=data.get("suggested_recipients", []),
            tone=data.get("tone", "professional"),
        )

    def generate_quick_replies(self, email: EmailMessage) -> List[Dict[str, str]]:
        """
        Generate quick reply options for an email.

        Args:
            email: The email to reply to

        Returns:
            List of reply options
        """
        prompt = SMART_REPLY_PROMPT.format(
            sender=email.sender,
            subject=email.subject,
            body=email.body[:4000],
        )

        try:
            data = self.llm.complete_json(prompt, temperature=0.5)
            return data.get("replies", [])
        except Exception as e:
            logger.error("Failed to generate quick replies: %s", e)
            return []

    def send_email(self, request: EmailSendRequest) -> bool:
        """
        Send an email.

        Args:
            request: Send request with email details

        Returns:
            True if sent successfully
        """
        draft = EmailDraft(
            to=request.to,
            cc=request.cc,
            bcc=request.bcc,
            subject=request.subject,
            body=request.body,
        )

        return self.email_tool.send_email(request.user_id, draft)

    def create_draft(
        self,
        user_id: str,
        draft_result: DraftResult,
        recipients: List[str],
    ) -> str:
        """
        Create a draft in the user's email (Gmail only).

        Args:
            user_id: The user ID
            draft_result: The drafted email
            recipients: Email recipients

        Returns:
            Draft ID
        """
        draft = EmailDraft(
            to=recipients,
            subject=draft_result.subject,
            body=draft_result.body,
        )

        return self.email_tool.create_draft(user_id, draft)

    def search_emails(self, request: EmailSearchRequest) -> List[EmailMessage]:
        """
        Search emails.

        Args:
            request: Search request

        Returns:
            List of matching emails
        """
        return self.email_tool.search_emails(
            user_id=request.user_id,
            query=request.query,
            limit=request.limit,
        )
