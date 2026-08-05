"""
Credential generation phase: Generate passwords and tokens for tools.

This is phase 2 of the provisioning workflow.
"""

from typing import Any, Callable, Dict, List, Optional

from ..models import (
    CredentialGenerationResult,
    GeneratedCredentials,
)
from ..shared.credential_store import CredentialStore
from ..shared.tool_manifest import ToolManifest


def run_credential_generation(
    agent_id: str,
    manifest: ToolManifest,
    credential_store: Optional[CredentialStore] = None,
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
    tool_names: Optional[List[str]] = None,
    fencing_token: Optional[int] = None,
) -> CredentialGenerationResult:
    """
    Execute the credential generation phase.

    Generates secure passwords/tokens for each tool in the manifest
    (or for an explicit frozen ``tool_names`` snapshot).

    Args:
        agent_id: Unique identifier for the agent
        manifest: Loaded tool manifest (used when ``tool_names`` is omitted)
        credential_store: Store for persisting credentials
        progress_callback: Callback(tool_name, done, total) for progress updates
        tool_names: Optional frozen name list; when set, overrides ``manifest.tools``
        fencing_token: Caller's fencing token (see ``shared.fencing``);
            ``None`` skips enforcement.

    Returns:
        CredentialGenerationResult with generated credentials per tool

    Preconditions:
        * ``agent_id`` is non-empty.
        * When ``tool_names`` is set, every entry is a non-empty string.
    Postconditions:
        * Each generated credential is stored in ``credential_store``.
        * Returned map is keyed by the same tool names that were processed.
    """
    assert agent_id, "agent_id must be non-empty"
    if tool_names is not None:
        assert all(isinstance(n, str) and n for n in tool_names), (
            "tool_names must be non-empty strings"
        )
        names = list(tool_names)
    else:
        names = [t.name for t in manifest.tools]

    cred_store = credential_store or CredentialStore()

    credentials: Dict[str, GeneratedCredentials] = {}
    total = len(names)

    for idx, tool_name in enumerate(names):
        if progress_callback:
            progress_callback(tool_name, idx, total)

        username = cred_store.generate_username(agent_id, tool_name)
        password = cred_store.generate_password()
        token = cred_store.generate_token() if _needs_token(tool_name) else None

        cred = GeneratedCredentials(
            tool_name=tool_name,
            username=username,
            password=password,
            token=token,
        )

        cred_store.store_credentials(
            agent_id=agent_id,
            tool_name=tool_name,
            credentials={
                "username": username,
                "password": password,
                "token": token,
            },
            fencing_token=fencing_token,
        )

        credentials[tool_name] = cred

    if progress_callback:
        progress_callback("complete", total, total)

    return CredentialGenerationResult(
        success=True,
        credentials=credentials,
    )


def _needs_token(tool_name: str) -> bool:
    """Determine if a tool needs a token in addition to password."""
    token_tools = {"git", "api", "oauth"}
    return tool_name.lower() in token_tools


def regenerate_credentials(
    agent_id: str,
    tool_name: str,
    credential_store: Optional[CredentialStore] = None,
) -> Optional[GeneratedCredentials]:
    """
    Regenerate credentials for a specific tool.

    Args:
        agent_id: Agent identifier
        tool_name: Tool to regenerate credentials for
        credential_store: Credential store instance

    Returns:
        New GeneratedCredentials or None on failure
    """
    cred_store = credential_store or CredentialStore()

    username = cred_store.generate_username(agent_id, tool_name)
    password = cred_store.generate_password()
    token = cred_store.generate_token() if _needs_token(tool_name) else None

    cred = GeneratedCredentials(
        tool_name=tool_name,
        username=username,
        password=password,
        token=token,
    )

    cred_store.store_credentials(
        agent_id=agent_id,
        tool_name=tool_name,
        credentials={
            "username": username,
            "password": password,
            "token": token,
        },
    )

    return cred


def get_stored_credentials(
    agent_id: str,
    credential_store: Optional[CredentialStore] = None,
) -> Dict[str, GeneratedCredentials]:
    """
    Retrieve previously stored credentials for an agent.

    Returns:
        Dict of tool_name -> GeneratedCredentials
    """
    cred_store = credential_store or CredentialStore()

    stored = cred_store.get_credentials(agent_id)
    if not stored:
        return {}

    credentials: Dict[str, GeneratedCredentials] = {}
    for tool_name, cred_data in stored.items():
        if not isinstance(cred_data, dict):
            continue
        credentials[tool_name] = GeneratedCredentials(
            tool_name=tool_name,
            username=cred_data.get("username"),
            password=cred_data.get("password"),
            token=cred_data.get("token"),
            ssh_private_key=cred_data.get("ssh_private_key"),
            ssh_public_key=cred_data.get("ssh_public_key"),
            connection_string=cred_data.get("connection_string"),
            extra=dict(cred_data.get("extra") or {}),
        )

    return credentials


def store_credentials_payload(
    agent_id: str,
    tool_name: str,
    credentials: Dict[str, Any],
    *,
    credential_store: Optional[CredentialStore] = None,
    fencing_token: Optional[int] = None,
) -> None:
    """Persist a full credentials dump (including enriched fields) to the store.

    Preconditions:
        * ``agent_id`` / ``tool_name`` are non-empty.
        * ``credentials`` is a mapping (may include connection_string / SSH / extra).
    Postconditions:
        * ``CredentialStore`` holds the durable fields for ``tool_name`` under ``agent_id``.
        * When ``fencing_token`` is given and stale, raises
          :class:`~agent_team_studio.agent_provisioning_team.shared.fencing.StaleFencingTokenError`.
    """
    assert agent_id, "agent_id must be non-empty"
    assert tool_name, "tool_name must be non-empty"
    cred_store = credential_store or CredentialStore()
    cred_store.store_credentials(
        agent_id=agent_id,
        tool_name=tool_name,
        credentials={
            "username": credentials.get("username"),
            "password": credentials.get("password"),
            "token": credentials.get("token"),
            "ssh_private_key": credentials.get("ssh_private_key"),
            "ssh_public_key": credentials.get("ssh_public_key"),
            "connection_string": credentials.get("connection_string"),
            "extra": dict(credentials.get("extra") or {}),
        },
        fencing_token=fencing_token,
    )
