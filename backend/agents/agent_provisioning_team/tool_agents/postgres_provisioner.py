"""
PostgreSQL provisioner tool agent.

Creates databases and users with scoped permissions.
"""

import os
from typing import Any, Dict, List, Optional

from ..models import (
    AccessTier,
    AccessVerification,
    DeprovisionResult,
    GeneratedCredentials,
    ToolProvisionResult,
)
from ..shared.access_policy import get_permissions, validate_permissions
from .base import BaseToolProvisioner

try:
    import psycopg2
    from psycopg2 import sql

    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False


class PostgresProvisionerTool(BaseToolProvisioner):
    """Tool agent for PostgreSQL database provisioning."""

    tool_name = "postgresql"

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        admin_user: Optional[str] = None,
        admin_password: Optional[str] = None,
    ) -> None:
        self.host = host or os.environ.get("POSTGRES_HOST", "localhost")
        self.port = port or int(os.environ.get("POSTGRES_PORT", "5432"))
        self.admin_user = admin_user or os.environ.get("POSTGRES_USER", "postgres")
        self.admin_password = admin_password or os.environ.get("POSTGRES_PASSWORD", "")
        self._provisioned: Dict[str, Dict[str, Any]] = {}

    def _get_admin_connection(self):
        """Get a connection with admin privileges."""
        if not HAS_PSYCOPG2:
            raise RuntimeError("psycopg2 is not installed")

        return psycopg2.connect(
            host=self.host,
            port=self.port,
            user=self.admin_user,
            password=self.admin_password,
            database="postgres",
        )

    def provision(
        self,
        agent_id: str,
        config: Dict[str, Any],
        credentials: GeneratedCredentials,
        access_tier: AccessTier,
    ) -> ToolProvisionResult:
        """Create a PostgreSQL database and user for the agent."""
        if not HAS_PSYCOPG2:
            return self._make_error_result("psycopg2 is not installed")

        try:
            db_prefix = config.get("database_prefix", "agent_")
            db_name = f"{db_prefix}{agent_id}".replace("-", "_")[:63]
            username = credentials.username or f"agent_{agent_id}".replace("-", "_")[:63]
            password = credentials.password

            if not password:
                return self._make_error_result("No password provided in credentials")

            conn = self._get_admin_connection()
            conn.autocommit = True
            cursor = conn.cursor()

            try:
                cursor.execute(
                    sql.SQL("CREATE USER {} WITH PASSWORD %s").format(sql.Identifier(username)),
                    [password],
                )
            except psycopg2.errors.DuplicateObject:
                cursor.execute(
                    sql.SQL("ALTER USER {} WITH PASSWORD %s").format(sql.Identifier(username)),
                    [password],
                )

            try:
                cursor.execute(
                    sql.SQL("CREATE DATABASE {} OWNER {}").format(
                        sql.Identifier(db_name),
                        sql.Identifier(username),
                    )
                )
            except psycopg2.errors.DuplicateDatabase:
                pass

            permissions = get_permissions("postgresql", access_tier)
            self._apply_permissions(cursor, db_name, username, permissions)

            cursor.close()
            conn.close()

            connection_string = (
                f"postgresql://{username}:{password}@{self.host}:{self.port}/{db_name}"
            )

            credentials.connection_string = connection_string
            credentials.extra["database"] = db_name
            credentials.extra["host"] = self.host
            credentials.extra["port"] = self.port

            self._provisioned[agent_id] = {
                "database": db_name,
                "username": username,
                "permissions": permissions,
            }

            return self._make_success_result(
                credentials=credentials,
                permissions=permissions,
                details={
                    "database": db_name,
                    "username": username,
                    "host": self.host,
                    "port": self.port,
                },
            )

        except Exception as e:
            return self._make_error_result(f"PostgreSQL provisioning error: {str(e)}")

    def _apply_permissions(
        self,
        cursor,
        db_name: str,
        username: str,
        permissions: List[str],
    ) -> None:
        """Apply permissions to the user on the database."""
        if "ALL PRIVILEGES" in permissions:
            cursor.execute(
                sql.SQL("GRANT ALL PRIVILEGES ON DATABASE {} TO {}").format(
                    sql.Identifier(db_name),
                    sql.Identifier(username),
                )
            )
        else:
            for perm in permissions:
                if perm in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    cursor.execute(
                        sql.SQL("GRANT {} ON ALL TABLES IN SCHEMA public TO {}").format(
                            sql.SQL(perm),
                            sql.Identifier(username),
                        )
                    )
                elif perm in ("CREATE", "DROP"):
                    cursor.execute(
                        sql.SQL("GRANT {} ON SCHEMA public TO {}").format(
                            sql.SQL(perm),
                            sql.Identifier(username),
                        )
                    )

    def verify_access(
        self,
        agent_id: str,
        expected_tier: AccessTier,
    ) -> AccessVerification:
        """Verify PostgreSQL access for the agent."""
        prov_info = self._provisioned.get(agent_id)

        if not prov_info:
            return self._make_verification(
                passed=False,
                expected_tier=expected_tier,
                actual_permissions=[],
                errors=[f"No PostgreSQL provisioning found for agent {agent_id}"],
            )

        actual_permissions = prov_info.get("permissions", [])
        passed, warnings = validate_permissions(
            "postgresql",
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
        """Remove PostgreSQL database and user."""
        if not HAS_PSYCOPG2:
            return DeprovisionResult(
                tool_name=self.tool_name,
                success=False,
                error="psycopg2 is not installed",
            )

        prov_info = self._provisioned.get(agent_id)
        if not prov_info:
            return DeprovisionResult(
                tool_name=self.tool_name,
                success=True,
                details={"message": "No database to remove"},
            )

        try:
            conn = self._get_admin_connection()
            conn.autocommit = True
            cursor = conn.cursor()

            db_name = prov_info["database"]
            username = prov_info["username"]

            cursor.execute(
                sql.SQL("""
                    SELECT pg_terminate_backend(pg_stat_activity.pid)
                    FROM pg_stat_activity
                    WHERE pg_stat_activity.datname = %s
                    AND pid <> pg_backend_pid()
                """),
                [db_name],
            )

            cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(db_name)))

            cursor.execute(sql.SQL("DROP USER IF EXISTS {}").format(sql.Identifier(username)))

            cursor.close()
            conn.close()

            del self._provisioned[agent_id]

            return DeprovisionResult(
                tool_name=self.tool_name,
                success=True,
                details={
                    "database_dropped": db_name,
                    "user_dropped": username,
                },
            )

        except Exception as e:
            return DeprovisionResult(
                tool_name=self.tool_name,
                success=False,
                error=str(e),
            )
