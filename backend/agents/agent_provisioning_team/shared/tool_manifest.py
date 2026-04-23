"""
YAML manifest loader and validator for tool configurations.

Manifests define which tools to provision and their configurations.

Validation is layered:

1. Top-level ``ToolManifest`` parsing via Pydantic.
2. Per-provisioner ``config`` validation via the ``PROVISIONER_CONFIG_SCHEMAS``
   registry (a lightweight discriminated union keyed on ``provisioner``).
   Unknown keys are rejected so typos don't silently no-op.
3. Manifest-level ``environment`` dict is checked against a
   secrets/metachar allowlist — manifests must not smuggle credentials
   through env vars.
4. Filesystem paths in provisioner configs (``workspace_path``, ``init_repos``)
   are checked for ``..`` traversal at manifest-parse time; the runtime
   containment check against each provisioner's ``workspace_base`` lives in
   :func:`assert_path_within_base`.

The allowlist constants (``_ENV_*``) below are the single source of truth for
any new provisioners that need to validate manifest-supplied env vars or
shell-like strings.
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_MANIFESTS_DIR = Path(__file__).parent.parent / "manifests"


# ---------------------------------------------------------------------------
# Filesystem path containment
# ---------------------------------------------------------------------------


def _reject_traversal_components(value: str, *, field: str) -> str:
    """Reject path strings whose components include ``..``.

    This runs at manifest-parse time — before any provisioner is instantiated
    — and doesn't know the workspace base. Its job is to bar the trivial
    escapes (``../../etc/passwd``). The runtime containment check against
    the actual workspace base is :func:`assert_path_within_base`.
    """
    parts = Path(value).parts
    if ".." in parts:
        raise ValueError(f"{field} must not contain '..' traversal components: {value!r}")
    return value


def _reject_path_separators(value: str, *, field: str) -> str:
    """Reject strings that are meant to be single flat segments (e.g. repo names)."""
    if not value:
        raise ValueError(f"{field} entry must be a non-empty string")
    if ".." in Path(value).parts or value == "..":
        raise ValueError(f"{field} entry must not traverse: {value!r}")
    for bad in ("/", "\\", os.sep):
        if bad in value:
            raise ValueError(
                f"{field} entry must be a flat name without path separators: {value!r}"
            )
    return value


def assert_path_within_base(path: str, base: str) -> Path:
    """Resolve ``path`` and assert it lives under ``base``; return the resolved Path.

    Used by :class:`GitProvisionerTool._do_provision` as defence-in-depth after
    manifest validation. The workspace base is only known at provisioner-
    construction time, so this check cannot be expressed as a Pydantic
    validator.

    Raises:
        ValueError: when the resolved ``path`` is not equal to, or a descendant
        of, the resolved ``base``.
    """
    resolved = Path(path).resolve()
    base_resolved = Path(base).resolve()
    try:
        resolved.relative_to(base_resolved)
    except ValueError as e:
        raise ValueError(f"path {path!r} escapes provisioner workspace base {base!r}") from e
    return resolved


# ---------------------------------------------------------------------------
# Per-provisioner config schemas (lightweight discriminated union)
# ---------------------------------------------------------------------------


class _ToolBaseConfig(BaseModel):
    """Base for provisioner config.

    ``extra="allow"`` for backwards compatibility with existing manifests
    that carry provisioner-specific knobs not yet in the typed schema. The
    declared fields below ARE strictly validated when present.
    """

    model_config = ConfigDict(extra="allow")


class DockerProvisionerConfig(_ToolBaseConfig):
    base_image: Optional[str] = None
    workspace_path: Optional[str] = None
    ssh_port: Optional[int] = Field(default=None, ge=1, le=65535)
    expose_ssh: bool = False
    init_command: Optional[str] = None
    environment: Dict[str, str] = Field(default_factory=dict)

    @field_validator("workspace_path")
    @classmethod
    def _workspace_path(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return v
        return _reject_traversal_components(v, field="workspace_path")


class PostgresProvisionerConfig(_ToolBaseConfig):
    database_prefix: str = "agent_"
    max_connections: Optional[int] = Field(default=None, ge=1, le=10000)
    schema_name: Optional[str] = None


class RedisProvisionerConfig(_ToolBaseConfig):
    key_prefix: Optional[str] = None
    namespace_prefix: Optional[str] = None
    enable_pubsub: bool = False
    max_memory_mb: Optional[int] = Field(default=None, ge=1)


class GitProvisionerConfig(_ToolBaseConfig):
    org: Optional[str] = None
    repo_prefix: str = "agent-"
    default_branch: str = "main"
    visibility: str = "private"
    generate_ssh_key: bool = False
    workspace_path: Optional[str] = None
    init_repos: List[str] = Field(default_factory=list)

    @field_validator("visibility")
    @classmethod
    def _visibility(cls, v: str) -> str:
        if v not in {"private", "internal", "public"}:
            raise ValueError("visibility must be one of: private, internal, public")
        return v

    @field_validator("workspace_path")
    @classmethod
    def _workspace_path(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return v
        return _reject_traversal_components(v, field="workspace_path")

    @field_validator("init_repos")
    @classmethod
    def _init_repos(cls, v: List[str]) -> List[str]:
        return [_reject_path_separators(r, field="init_repos") for r in v]


class GenericProvisionerConfig(_ToolBaseConfig):
    kind: str = "generic"
    token_type: Optional[str] = None
    permissions: List[str] = Field(default_factory=list)
    params: Dict[str, Any] = Field(default_factory=dict)


PROVISIONER_CONFIG_SCHEMAS: Dict[str, Type[_ToolBaseConfig]] = {
    "docker_provisioner": DockerProvisionerConfig,
    "postgres_provisioner": PostgresProvisionerConfig,
    "redis_provisioner": RedisProvisionerConfig,
    "git_provisioner": GitProvisionerConfig,
    "generic_provisioner": GenericProvisionerConfig,
}


def validate_provisioner_config(provisioner: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a raw ``config`` dict against its provisioner schema.

    Returns the normalized (Pydantic-dumped) dict. Raises ``ValueError`` on
    unknown provisioners or invalid fields.
    """
    schema = PROVISIONER_CONFIG_SCHEMAS.get(provisioner)
    if schema is None:
        raise ValueError(f"Unknown provisioner: {provisioner}")
    try:
        return schema.model_validate(raw or {}).model_dump()
    except Exception as e:  # pydantic ValidationError
        raise ValueError(f"Invalid config for {provisioner}: {e}") from e


# ---------------------------------------------------------------------------
# Environment variable allowlist
# ---------------------------------------------------------------------------

_ENV_KEY_SECRET_SUBSTRINGS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "privatekey",
    "credential",
    "auth",
)
_ENV_KEY_MAX_LEN = 128
_ENV_VALUE_MAX_LEN = 4096
_ENV_METACHARS = set("`$\n\r\x00")


def validate_manifest_environment(env: Dict[str, str]) -> Dict[str, str]:
    """Reject manifest environment variables that smell like smuggled secrets.

    Policy:
      * Keys matching ``UPPER_SNAKE`` only.
      * Keys must not contain any ``_ENV_KEY_SECRET_SUBSTRINGS`` substring.
      * Values capped in length and stripped of shell-metachars that would
        allow command injection if a downstream consumer naively expanded them.
    """
    normalized: Dict[str, str] = {}
    for key, value in env.items():
        if not isinstance(key, str) or not key:
            raise ValueError("Manifest environment keys must be non-empty strings")
        if len(key) > _ENV_KEY_MAX_LEN:
            raise ValueError(f"Env key too long: {key[:32]}…")
        if not all(c.isupper() or c.isdigit() or c == "_" for c in key):
            raise ValueError(f"Env key '{key}' must be UPPER_SNAKE_CASE (A-Z, 0-9, _)")
        low = key.lower()
        if any(s in low for s in _ENV_KEY_SECRET_SUBSTRINGS):
            raise ValueError(
                f"Env key '{key}' looks like a secret — store secrets in the "
                "credential store, not the manifest"
            )
        if value is None:
            value = ""
        if not isinstance(value, (str, int, float, bool)):
            raise ValueError(f"Env value for '{key}' must be scalar")
        s = str(value)
        if len(s) > _ENV_VALUE_MAX_LEN:
            raise ValueError(f"Env value for '{key}' exceeds {_ENV_VALUE_MAX_LEN} chars")
        if any(c in _ENV_METACHARS for c in s):
            raise ValueError(f"Env value for '{key}' contains disallowed shell metacharacters")
        normalized[key] = s
    return normalized


class ToolOnboardingConfig(BaseModel):
    """Onboarding documentation config for a tool."""

    description: str = Field(default="", description="Tool description")
    env_var: Optional[str] = Field(default=None, description="Primary environment variable")
    getting_started: str = Field(default="", description="Getting started guide")


class ToolDefinition(BaseModel):
    """Definition of a single tool in the manifest."""

    name: str = Field(..., description="Tool name")
    provisioner: str = Field(..., description="Provisioner to use")
    access_level: str = Field(default="standard", description="Access level")
    config: Dict[str, Any] = Field(default_factory=dict, description="Tool-specific config")
    onboarding: ToolOnboardingConfig = Field(
        default_factory=ToolOnboardingConfig,
        description="Onboarding docs",
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not v or not v.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"Invalid tool name: {v}")
        return v.lower()

    @field_validator("provisioner")
    @classmethod
    def validate_provisioner(cls, v: str) -> str:
        if v not in PROVISIONER_CONFIG_SCHEMAS:
            raise ValueError(
                f"Unknown provisioner: {v}. Valid: {sorted(PROVISIONER_CONFIG_SCHEMAS)}"
            )
        return v

    @model_validator(mode="after")
    def _validate_typed_config(self) -> "ToolDefinition":
        # Replaces the untyped Dict[str, Any] with a validated, normalized dict.
        object.__setattr__(
            self, "config", validate_provisioner_config(self.provisioner, self.config)
        )
        return self


class ToolManifest(BaseModel):
    """Complete tool manifest configuration."""

    version: str = Field(default="1.0", description="Manifest version")
    base_image: str = Field(default="python:3.11-slim", description="Docker base image")
    tools: List[ToolDefinition] = Field(default_factory=list, description="Tools to provision")
    environment: Dict[str, str] = Field(
        default_factory=dict,
        description="Additional environment variables",
    )

    @field_validator("environment")
    @classmethod
    def _validate_env(cls, v: Dict[str, str]) -> Dict[str, str]:
        return validate_manifest_environment(v)

    @property
    def tool_names(self) -> List[str]:
        """Get list of tool names."""
        return [t.name for t in self.tools]

    def get_tool(self, name: str) -> Optional[ToolDefinition]:
        """Get a tool definition by name."""
        for tool in self.tools:
            if tool.name == name:
                return tool
        return None


def load_manifest(
    manifest_path: str,
    manifests_dir: Optional[Path] = None,
) -> ToolManifest:
    """Load and validate a tool manifest from YAML.

    Args:
        manifest_path: Path to manifest file (relative to manifests_dir or absolute)
        manifests_dir: Base directory for manifests (defaults to package manifests/)

    Returns:
        Validated ToolManifest

    Raises:
        FileNotFoundError: If manifest file doesn't exist
        ValueError: If manifest is invalid
    """
    manifests_dir = manifests_dir or DEFAULT_MANIFESTS_DIR

    path = Path(manifest_path)
    if not path.is_absolute():
        path = manifests_dir / manifest_path

    if not path.exists():
        raise FileNotFoundError(f"Manifest not found: {path}")

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML in manifest: {e}") from e

    if data is None:
        data = {}

    try:
        return ToolManifest(**data)
    except Exception as e:
        raise ValueError(f"Invalid manifest structure: {e}") from e


def validate_manifest(manifest_path: str) -> List[str]:
    """Validate a manifest and return any errors.

    Returns:
        List of error messages (empty if valid)
    """
    errors: List[str] = []

    try:
        manifest = load_manifest(manifest_path)

        if not manifest.tools:
            errors.append("Manifest has no tools defined")

        tool_names = [t.name for t in manifest.tools]
        if len(tool_names) != len(set(tool_names)):
            errors.append("Duplicate tool names in manifest")

    except FileNotFoundError as e:
        errors.append(str(e))
    except ValueError as e:
        errors.append(str(e))

    return errors
