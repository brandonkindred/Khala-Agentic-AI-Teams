"""
Redis provisioner tool agent.

Sets up Redis ACL with key prefix restrictions.
"""

import os
from typing import Any, Dict, List, Optional, Tuple

from ..models import (
    AccessTier,
    AccessVerification,
    DeprovisionResult,
    GeneratedCredentials,
    ToolProvisionResult,
)
from ..shared.access_policy import get_permissions, validate_permissions
from ..shared.provisioner_state import ProvisionerStateStore
from .base import BaseToolProvisioner

try:
    import redis

    HAS_REDIS = True
except ImportError:
    HAS_REDIS = False


class RedisProvisionerTool(BaseToolProvisioner):
    """Tool agent for Redis provisioning with ACL."""

    tool_name = "redis"

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        admin_password: Optional[str] = None,
    ) -> None:
        self.host = host or os.environ.get("REDIS_HOST", "localhost")
        self.port = port or int(os.environ.get("REDIS_PORT", "6379"))
        self.admin_password = admin_password or os.environ.get("REDIS_PASSWORD")
        self._state = ProvisionerStateStore("redis_provisioner")

    def _get_admin_client(self):
        """Get a Redis client with admin privileges."""
        if not HAS_REDIS:
            raise RuntimeError("redis package is not installed")

        return redis.Redis(
            host=self.host,
            port=self.port,
            password=self.admin_password,
            decode_responses=True,
        )

    def provision(
        self,
        agent_id: str,
        config: Dict[str, Any],
        credentials: GeneratedCredentials,
        access_tier: AccessTier,
    ) -> ToolProvisionResult:
        """Create a Redis ACL user with key prefix restrictions."""
        if not HAS_REDIS:
            return self._make_error_result("redis package is not installed")

        return self.run_idempotent(
            agent_id,
            credentials=credentials,
            create=lambda _register: self._do_provision(
                agent_id, config, credentials, access_tier
            ),
            reuse=lambda existing: self._on_reuse(existing, credentials),
        )

    def _do_provision(
        self,
        agent_id: str,
        config: Dict[str, Any],
        credentials: GeneratedCredentials,
        access_tier: AccessTier,
    ) -> Tuple[List[str], Dict[str, Any]]:
        key_prefix = config.get("key_prefix", f"agent:{agent_id}:")
        username = credentials.username or f"agent_{agent_id}".replace("-", "_")
        password = credentials.password

        if not password:
            raise ValueError("No password provided in credentials")

        client = self._get_admin_client()

        permissions = get_permissions("redis", access_tier)
        acl_rules = self._build_acl_rules(permissions, key_prefix)

        try:
            client.acl_deluser(username)
        except redis.exceptions.ResponseError:
            pass

        client.acl_setuser(
            username,
            enabled=True,
            passwords=[f"+{password}"],
            keys=[f"{key_prefix}*"],
            commands=acl_rules,
        )

        connection_url = f"redis://{username}:{password}@{self.host}:{self.port}"

        credentials.connection_string = connection_url
        credentials.extra["key_prefix"] = key_prefix
        credentials.extra["host"] = self.host
        credentials.extra["port"] = self.port

        details = {
            "username": username,
            "key_prefix": key_prefix,
            "host": self.host,
            "port": self.port,
            "permissions": permissions,
        }
        return permissions, details

    def _on_reuse(
        self,
        existing: Dict[str, Any],
        credentials: GeneratedCredentials,
    ) -> List[str]:
        credentials.extra.setdefault("key_prefix", existing.get("key_prefix", ""))
        credentials.extra.setdefault("host", self.host)
        credentials.extra.setdefault("port", self.port)
        return list(existing.get("permissions", []))

    def _build_acl_rules(
        self,
        permissions: List[str],
        key_prefix: str,
    ) -> List[str]:
        """Build Redis ACL command rules from permission list."""
        if "+@all" in permissions:
            return ["+@all"]

        command_map = {
            "GET": "+get",
            "SET": "+set",
            "DEL": "+del",
            "KEYS": "+keys",
            "EXISTS": "+exists",
            "TYPE": "+type",
            "EXPIRE": "+expire",
            "TTL": "+ttl",
            "PUBLISH": "+publish",
            "SUBSCRIBE": "+subscribe",
        }

        rules = ["-@all"]
        for perm in permissions:
            if perm in command_map:
                rules.append(command_map[perm])

        rules.append("+ping")
        rules.append("+echo")
        rules.append("+auth")

        return rules

    def verify_access(
        self,
        agent_id: str,
        expected_tier: AccessTier,
    ) -> AccessVerification:
        """Verify Redis ACL access for the agent."""
        prov_info = self._state.get(agent_id)

        if not prov_info:
            return self._make_verification(
                passed=False,
                expected_tier=expected_tier,
                actual_permissions=[],
                errors=[f"No Redis provisioning found for agent {agent_id}"],
            )

        actual_permissions = prov_info.get("permissions", [])
        passed, warnings = validate_permissions(
            "redis",
            expected_tier,
            actual_permissions,
        )

        return self._make_verification(
            passed=passed,
            expected_tier=expected_tier,
            actual_permissions=actual_permissions,
            warnings=warnings,
        )

    def deprovision(self, agent_id: str) -> DeprovisionResult:
        """Remove Redis ACL user."""
        if not HAS_REDIS:
            return DeprovisionResult(
                tool_name=self.tool_name,
                success=False,
                error="redis package is not installed",
            )

        prov_info = self._state.get(agent_id)
        if not prov_info:
            return DeprovisionResult(
                tool_name=self.tool_name,
                success=True,
                details={"message": "No Redis ACL to remove"},
            )

        try:
            client = self._get_admin_client()
            username = prov_info["username"]

            try:
                client.acl_deluser(username)
            except redis.exceptions.ResponseError:
                pass

            self._state.delete(agent_id)

            return DeprovisionResult(
                tool_name=self.tool_name,
                success=True,
                details={"user_deleted": username},
            )

        except Exception as e:
            return DeprovisionResult(
                tool_name=self.tool_name,
                success=False,
                error=str(e),
            )
