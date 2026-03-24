"""
YAML manifest loader and validator for tool configurations.

Manifests define which tools to provision and their configurations.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field, field_validator

DEFAULT_MANIFESTS_DIR = Path(__file__).parent.parent / "manifests"


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
        valid_provisioners = {
            "docker_provisioner",
            "postgres_provisioner",
            "git_provisioner",
            "redis_provisioner",
            "generic_provisioner",
        }
        if v not in valid_provisioners:
            raise ValueError(f"Unknown provisioner: {v}. Valid: {valid_provisioners}")
        return v


class ToolManifest(BaseModel):
    """Complete tool manifest configuration."""

    version: str = Field(default="1.0", description="Manifest version")
    base_image: str = Field(default="python:3.11-slim", description="Docker base image")
    tools: List[ToolDefinition] = Field(default_factory=list, description="Tools to provision")
    environment: Dict[str, str] = Field(
        default_factory=dict,
        description="Additional environment variables",
    )

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
