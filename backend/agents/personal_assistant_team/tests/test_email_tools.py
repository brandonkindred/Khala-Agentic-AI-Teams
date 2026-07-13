"""Tests for ``EmailToolAgent``'s IMAP connection handling.

The class deliberately never caches a connection on ``self`` (see
``_open_connection``'s docstring): it is shared across concurrent callers via
``core.get_orchestrator()`` (thread-mode dispatch and Temporal's activity
executor both route through the same orchestrator singleton), so a per-
instance "current connection" cache would let one user's request observe
another user's authenticated IMAP socket. These tests pin that contract down.
"""

from __future__ import annotations

import threading
import time

import pytest

from ..tools.email_tools import EmailToolAgent, EmailToolError


class _FakeCredentialStore:
    def __init__(self, credentials=None):
        self._credentials = credentials or {}

    def get_email_credentials(self, user_id):
        return self._credentials.get(user_id)


def _imap_creds(user_id):
    return {
        "provider": "imap",
        "host": "imap.example.com",
        "port": 993,
        "username": f"{user_id}@example.com",
        "password": "secret",
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
    }


class _FakeIMAPConnection:
    """Stand-in for ``imaplib.IMAP4_SSL`` recording login/logout calls."""

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.logged_in_as = None
        self.logged_out = False
        self.authenticated_with = None

    def login(self, username, password):
        self.logged_in_as = username

    def authenticate(self, mechanism, callback):
        self.authenticated_with = (mechanism, callback(None))

    def logout(self):
        self.logged_out = True

    def select(self, folder):
        pass

    def search(self, charset, criteria):
        return "OK", [b""]


def test_open_connection_uses_stored_imap_credentials(monkeypatch):
    store = _FakeCredentialStore({"u1": _imap_creds("u1")})
    agent = EmailToolAgent(credential_store=store)
    monkeypatch.setattr(
        "personal_assistant_team.tools.email_tools.imaplib.IMAP4_SSL", _FakeIMAPConnection
    )

    connection = agent._open_connection("u1")

    assert isinstance(connection, _FakeIMAPConnection)
    assert connection.host == "imap.example.com"
    assert connection.logged_in_as == "u1@example.com"


def test_open_connection_raises_without_stored_credentials():
    agent = EmailToolAgent(credential_store=_FakeCredentialStore())

    with pytest.raises(EmailToolError, match="No email credentials"):
        agent._open_connection("missing-user")


def test_open_connection_wraps_login_failures(monkeypatch):
    store = _FakeCredentialStore({"u1": _imap_creds("u1")})
    agent = EmailToolAgent(credential_store=store)

    class _BoomConnection(_FakeIMAPConnection):
        def login(self, username, password):
            raise RuntimeError("bad password")

    monkeypatch.setattr(
        "personal_assistant_team.tools.email_tools.imaplib.IMAP4_SSL", _BoomConnection
    )

    with pytest.raises(EmailToolError, match="IMAP connection failed"):
        agent._open_connection("u1")


def test_open_connection_delegates_oauth_credentials_to_oauth_path(monkeypatch):
    store = _FakeCredentialStore(
        {
            "u1": {
                "provider": "oauth",
                "provider_type": "gmail",
                "access_token": "tok",
                "email": "u1@gmail.com",
            }
        }
    )
    agent = EmailToolAgent(credential_store=store)
    monkeypatch.setattr(
        "personal_assistant_team.tools.email_tools.imaplib.IMAP4_SSL", _FakeIMAPConnection
    )

    connection = agent._open_connection("u1")

    assert connection.host == "imap.gmail.com"
    assert connection.authenticated_with[0] == "XOAUTH2"


def test_open_oauth_connection_raises_without_access_token(monkeypatch):
    agent = EmailToolAgent(credential_store=_FakeCredentialStore())
    monkeypatch.setattr(
        "personal_assistant_team.tools.email_tools.imaplib.IMAP4_SSL", _FakeIMAPConnection
    )

    with pytest.raises(EmailToolError, match="No access token"):
        agent._open_oauth_connection("u1", {"provider_type": "gmail"})


def test_connect_imap_opens_and_logs_out(monkeypatch):
    agent = EmailToolAgent(credential_store=_FakeCredentialStore())
    fake_connection = _FakeIMAPConnection("h", 993)
    monkeypatch.setattr(
        agent, "_open_connection", lambda user_id, credentials=None: fake_connection
    )

    assert agent.connect_imap("u1") is True
    assert fake_connection.logged_out is True


def test_connect_imap_swallows_logout_errors(monkeypatch):
    agent = EmailToolAgent(credential_store=_FakeCredentialStore())

    class _BadLogout(_FakeIMAPConnection):
        def logout(self):
            raise RuntimeError("connection already closed")

    fake_connection = _BadLogout("h", 993)
    monkeypatch.setattr(
        agent, "_open_connection", lambda user_id, credentials=None: fake_connection
    )

    assert agent.connect_imap("u1") is True


def test_connect_imap_propagates_connection_failure(monkeypatch):
    agent = EmailToolAgent(credential_store=_FakeCredentialStore())

    def _raise(user_id, credentials=None):
        raise EmailToolError("IMAP connection failed: boom")

    monkeypatch.setattr(agent, "_open_connection", _raise)

    with pytest.raises(EmailToolError, match="boom"):
        agent.connect_imap("u1")


def _raw_email(owner: str) -> bytes:
    return (
        f"From: {owner}@example.com\r\n"
        f"To: someone@example.com\r\n"
        f"Subject: hello from {owner}\r\n"
        "Date: Mon, 1 Jan 2024 00:00:00 +0000\r\n"
        "\r\n"
        f"body for {owner}\r\n"
    ).encode()


class _InboxConnection:
    """Fake IMAP connection with one message, owned by ``owner``."""

    def __init__(self, owner: str, delay: float = 0.0):
        self.owner = owner
        self._delay = delay
        self.logged_out = False
        self.selected_folder = None

    def select(self, folder):
        self.selected_folder = folder

    def search(self, charset, criteria):
        if self._delay:
            time.sleep(self._delay)
        return "OK", [f"{self.owner}-1".encode()]

    def fetch(self, msg_id, parts):
        return "OK", [(b"1 (RFC822 {123}", _raw_email(self.owner))]

    def logout(self):
        self.logged_out = True


def test_fetch_inbox_uses_and_closes_its_own_connection(monkeypatch):
    agent = EmailToolAgent(credential_store=_FakeCredentialStore())
    connection = _InboxConnection("u1")
    monkeypatch.setattr(agent, "_open_connection", lambda user_id, credentials=None: connection)

    messages = agent.fetch_inbox("u1")

    assert len(messages) == 1
    assert messages[0].sender == "u1@example.com"
    assert connection.selected_folder == "INBOX"
    assert connection.logged_out is True


def test_fetch_inbox_closes_connection_even_on_failure(monkeypatch):
    agent = EmailToolAgent(credential_store=_FakeCredentialStore())

    class _BoomConnection(_InboxConnection):
        def search(self, charset, criteria):
            raise RuntimeError("server hung up")

    connection = _BoomConnection("u1")
    monkeypatch.setattr(agent, "_open_connection", lambda user_id, credentials=None: connection)

    with pytest.raises(EmailToolError, match="Failed to fetch emails"):
        agent.fetch_inbox("u1")

    assert connection.logged_out is True


def test_search_emails_uses_and_closes_its_own_connection(monkeypatch):
    agent = EmailToolAgent(credential_store=_FakeCredentialStore())
    connection = _InboxConnection("u1")
    monkeypatch.setattr(agent, "_open_connection", lambda user_id, credentials=None: connection)

    messages = agent.search_emails("u1", "FROM someone")

    assert len(messages) == 1
    assert connection.logged_out is True


def test_search_emails_closes_connection_even_on_failure(monkeypatch):
    agent = EmailToolAgent(credential_store=_FakeCredentialStore())

    class _BoomConnection(_InboxConnection):
        def search(self, charset, criteria):
            raise RuntimeError("server hung up")

    connection = _BoomConnection("u1")
    monkeypatch.setattr(agent, "_open_connection", lambda user_id, credentials=None: connection)

    with pytest.raises(EmailToolError, match="Search failed"):
        agent.search_emails("u1", "FROM someone")

    assert connection.logged_out is True


def test_open_oauth_connection_wraps_authenticate_failures(monkeypatch):
    agent = EmailToolAgent(credential_store=_FakeCredentialStore())

    class _BoomAuth(_FakeIMAPConnection):
        def authenticate(self, mechanism, callback):
            raise RuntimeError("token rejected")

    monkeypatch.setattr("personal_assistant_team.tools.email_tools.imaplib.IMAP4_SSL", _BoomAuth)

    with pytest.raises(EmailToolError, match="OAuth connection failed"):
        agent._open_oauth_connection("u1", {"provider_type": "gmail", "access_token": "tok"})


def test_fetch_inbox_swallows_logout_error_in_finally(monkeypatch):
    agent = EmailToolAgent(credential_store=_FakeCredentialStore())

    class _BadLogoutConnection(_InboxConnection):
        def logout(self):
            raise RuntimeError("already closed")

    connection = _BadLogoutConnection("u1")
    monkeypatch.setattr(agent, "_open_connection", lambda user_id, credentials=None: connection)

    # Must not raise even though logout() itself fails.
    messages = agent.fetch_inbox("u1")
    assert len(messages) == 1


def test_search_emails_swallows_logout_error_in_finally(monkeypatch):
    agent = EmailToolAgent(credential_store=_FakeCredentialStore())

    class _BadLogoutConnection(_InboxConnection):
        def logout(self):
            raise RuntimeError("already closed")

    connection = _BadLogoutConnection("u1")
    monkeypatch.setattr(agent, "_open_connection", lambda user_id, credentials=None: connection)

    messages = agent.search_emails("u1", "FROM someone")
    assert len(messages) == 1


def _multipart_raw_email(owner: str) -> bytes:
    return (
        f"From: {owner}@example.com\r\n"
        f"To: someone@example.com\r\n"
        f"Cc: cc@example.com\r\n"
        f"Subject: hello from {owner}\r\n"
        "Date: Mon, 1 Jan 2024 00:00:00 +0000\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/alternative; boundary="BOUNDARY"\r\n'
        "\r\n"
        "--BOUNDARY\r\n"
        "Content-Type: text/plain\r\n"
        "\r\n"
        f"plain body for {owner}\r\n"
        "--BOUNDARY\r\n"
        "Content-Type: text/html\r\n"
        "\r\n"
        f"<p>html body for {owner}</p>\r\n"
        "--BOUNDARY--\r\n"
    ).encode()


class _MultipartInboxConnection(_InboxConnection):
    """Fake IMAP connection whose one message is multipart with a Cc header."""

    def fetch(self, msg_id, parts):
        return "OK", [(b"1 (RFC822 {123} FLAGS (\\Seen)", _multipart_raw_email(self.owner))]


def test_fetch_inbox_parses_multipart_message_with_html_body_and_seen_flag(monkeypatch):
    agent = EmailToolAgent(credential_store=_FakeCredentialStore())
    connection = _MultipartInboxConnection("u1")
    monkeypatch.setattr(agent, "_open_connection", lambda user_id, credentials=None: connection)

    messages = agent.fetch_inbox("u1", unread_only=True)

    assert len(messages) == 1
    message = messages[0]
    assert message.body.strip() == "plain body for u1"
    assert message.html_body.strip() == "<p>html body for u1</p>"
    assert message.cc == ["cc@example.com"]
    assert message.is_read is True


def test_fetch_inbox_skips_messages_with_missing_data(monkeypatch):
    agent = EmailToolAgent(credential_store=_FakeCredentialStore())

    class _MissingDataConnection(_InboxConnection):
        def fetch(self, msg_id, parts):
            return "OK", [None]

    connection = _MissingDataConnection("u1")
    monkeypatch.setattr(agent, "_open_connection", lambda user_id, credentials=None: connection)

    assert agent.fetch_inbox("u1") == []


def test_fetch_inbox_reraises_email_tool_error_from_search_without_rewrapping(monkeypatch):
    agent = EmailToolAgent(credential_store=_FakeCredentialStore())

    class _BoomConnection(_InboxConnection):
        def search(self, charset, criteria):
            raise EmailToolError("connection dropped mid-search")

    connection = _BoomConnection("u1")
    monkeypatch.setattr(agent, "_open_connection", lambda user_id, credentials=None: connection)

    with pytest.raises(EmailToolError, match="connection dropped mid-search"):
        agent.fetch_inbox("u1")


def test_search_emails_skips_messages_with_missing_data(monkeypatch):
    agent = EmailToolAgent(credential_store=_FakeCredentialStore())

    class _MissingDataConnection(_InboxConnection):
        def fetch(self, msg_id, parts):
            return "OK", [None]

    connection = _MissingDataConnection("u1")
    monkeypatch.setattr(agent, "_open_connection", lambda user_id, credentials=None: connection)

    assert agent.search_emails("u1", "FROM someone") == []


def test_search_emails_parses_multipart_message(monkeypatch):
    agent = EmailToolAgent(credential_store=_FakeCredentialStore())
    connection = _MultipartInboxConnection("u1")
    monkeypatch.setattr(agent, "_open_connection", lambda user_id, credentials=None: connection)

    messages = agent.search_emails("u1", "FROM someone")

    assert len(messages) == 1
    assert messages[0].body.strip() == "plain body for u1"


def test_search_emails_reraises_email_tool_error_without_rewrapping(monkeypatch):
    agent = EmailToolAgent(credential_store=_FakeCredentialStore())

    class _BoomConnection(_InboxConnection):
        def search(self, charset, criteria):
            raise EmailToolError("connection dropped mid-search")

    connection = _BoomConnection("u1")
    monkeypatch.setattr(agent, "_open_connection", lambda user_id, credentials=None: connection)

    with pytest.raises(EmailToolError, match="connection dropped mid-search"):
        agent.search_emails("u1", "FROM someone")


def test_no_shared_connection_state_on_the_agent_instance():
    # Regression guard for the removed `_imap_connection`/`_current_user`
    # instance attributes: the class must own no per-"current user" cache.
    agent = EmailToolAgent(credential_store=_FakeCredentialStore())

    assert not hasattr(agent, "_imap_connection")
    assert not hasattr(agent, "_current_user")


def test_concurrent_fetch_inbox_for_different_users_never_cross_contaminates(monkeypatch):
    # Regression test for the cross-user IMAP data leak: before the fix, a
    # single cached `self._imap_connection`/`self._current_user` on a shared
    # EmailToolAgent instance meant one user's slower call could have its
    # connection silently reassigned mid-flight by a concurrent call for a
    # different user. Give user "slow" an artificial delay inside `search` so
    # its request is still in flight while "fast" starts and finishes,
    # forcing the interleaving that used to trigger the leak.
    agent = EmailToolAgent(credential_store=_FakeCredentialStore())

    def fake_open_connection(user_id, credentials=None):
        delay = 0.05 if user_id == "slow" else 0.0
        return _InboxConnection(user_id, delay=delay)

    monkeypatch.setattr(agent, "_open_connection", fake_open_connection)

    results = {}

    def _run(user_id):
        results[user_id] = agent.fetch_inbox(user_id)

    slow_thread = threading.Thread(target=_run, args=("slow",))
    slow_thread.start()
    time.sleep(0.01)  # let "slow" enter its delayed search first
    fast_thread = threading.Thread(target=_run, args=("fast",))
    fast_thread.start()
    fast_thread.join()
    slow_thread.join()

    assert results["slow"][0].sender == "slow@example.com"
    assert results["fast"][0].sender == "fast@example.com"
