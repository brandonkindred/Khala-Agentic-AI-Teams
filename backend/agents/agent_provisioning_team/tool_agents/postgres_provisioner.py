"""
PostgreSQL provisioner tool agent.

Creates databases and users with scoped permissions.
"""

import os
from typing import Any, Dict, List, Optional, Tuple

from ..models import (
    AccessVerification,
    DeprovisionResult,
    GeneratedCredentials,
    ToolProvisionResult,
)
from ..shared.fencing import StaleFencingTokenError
from ..shared.provisioner_state import ProvisionerStateStore
from .base import BaseToolProvisioner, CompensationRegistrar

# Every sandbox is provisioned with full DB privileges (#456). Recorded
# here so onboarding docs and the access-audit phase keep listing what
# the agent actually has.
_FULL_POSTGRES_PERMISSIONS: list[str] = ["ALL PRIVILEGES"]

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
        # Persistent idempotency store — survives process restarts.
        self._state = ProvisionerStateStore("postgres_provisioner")

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
        fencing_token: Optional[int] = None,
    ) -> ToolProvisionResult:
        """Create a PostgreSQL database and user for the agent."""
        if not HAS_PSYCOPG2:
            return self._make_error_result("psycopg2 is not installed")

        return self.run_idempotent(
            agent_id,
            credentials=credentials,
            create=lambda register_compensation: self._do_provision(
                agent_id, config, credentials, register_compensation
            ),
            reuse=lambda existing: self._on_reuse(existing, credentials),
            fencing_token=fencing_token,
        )

    def _do_provision(
        self,
        agent_id: str,
        config: Dict[str, Any],
        credentials: GeneratedCredentials,
        register_compensation: CompensationRegistrar,
    ) -> Tuple[List[str], Dict[str, Any]]:
        db_prefix = config.get("database_prefix", "agent_")
        db_name = f"{db_prefix}{agent_id}".replace("-", "_")[:63]
        username = credentials.username or f"agent_{agent_id}".replace("-", "_")[:63]
        password = credentials.password

        if not password:
            raise ValueError("No password provided in credentials")

        conn = self._get_admin_connection()
        conn.autocommit = True
        cursor = conn.cursor()

        role_existed = False
        try:
            cursor.execute(
                sql.SQL("CREATE USER {} WITH PASSWORD %s").format(sql.Identifier(username)),
                [password],
            )
        except psycopg2.errors.DuplicateObject:
            role_existed = True
            cursor.execute(
                sql.SQL("ALTER USER {} WITH PASSWORD %s").format(sql.Identifier(username)),
                [password],
            )
        if not role_existed:
            # We created the role, so we own the rollback for it.
            register_compensation("postgres.drop_role", {"username": username})

        db_existed = False
        try:
            cursor.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(db_name),
                    sql.Identifier(username),
                )
            )
        except psycopg2.errors.DuplicateDatabase:
            db_existed = True
        if not db_existed:
            # Registered second so LIFO replay drops the DB before the role
            # (the DB is owned by the role — required ordering).
            register_compensation("postgres.drop_database", {"database": db_name})

        permissions = list(_FULL_POSTGRES_PERMISSIONS)
        self._apply_permissions(cursor, db_name, username, permissions)

        cursor.close()
        conn.close()

        connection_string = f"postgresql://{username}:{password}@{self.host}:{self.port}/{db_name}"

        credentials.connection_string = connection_string
        credentials.extra["database"] = db_name
        credentials.extra["host"] = self.host
        credentials.extra["port"] = self.port

        details = {
            "database": db_name,
            "username": username,
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
        credentials.extra.setdefault("database", existing["database"])
        credentials.extra.setdefault("host", self.host)
        credentials.extra.setdefault("port", self.port)
        return existing.get("permissions", list(_FULL_POSTGRES_PERMISSIONS))

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

    def verify_access(self, agent_id: str) -> AccessVerification:
        """Surface the recorded PostgreSQL permissions for the agent."""
        prov_info = self._state.get(agent_id)

        if not prov_info:
            return self._make_verification(
                passed=False,
                actual_permissions=[],
                errors=[f"No PostgreSQL provisioning found for agent {agent_id}"],
            )

        actual_permissions = prov_info.get("permissions", list(_FULL_POSTGRES_PERMISSIONS))

        return self._make_verification(
            passed=True,
            actual_permissions=actual_permissions,
        )

    def deprovision(self, agent_id: str, fencing_token: Optional[int] = None) -> DeprovisionResult:
        """Remove PostgreSQL database and user.

        ``fencing_token``, when given, is checked *before* the real
        ``DROP DATABASE``/``DROP USER`` statements run, not just before the
        final state persist, so a stale caller is rejected before it can
        touch the live database.
        """
        if not HAS_PSYCOPG2:
            return DeprovisionResult(
                tool_name=self.tool_name,
                success=False,
                error="psycopg2 is not installed",
            )

        if fencing_token is not None:
            self._state.check_fencing_token(agent_id, fencing_token)

        prov_info = self._state.get(agent_id)
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

            self._drop_database(cursor, db_name)
            self._drop_role(cursor, username)

            cursor.close()
            conn.close()

            self._state.delete(agent_id, fencing_token=fencing_token)

            return DeprovisionResult(
                tool_name=self.tool_name,
                success=True,
                details={
                    "database_dropped": db_name,
                    "user_dropped": username,
                },
            )

        except StaleFencingTokenError:
            # A stale-token rejection from the fenced _state.delete is an
            # ownership error, not an infra failure: propagate it (non-retryable)
            # instead of folding it into a soft success=False result.
            raise
        except Exception as e:
            return DeprovisionResult(
                tool_name=self.tool_name,
                success=False,
                error=str(e),
            )

    def replay_compensation(
        self,
        agent_id: str,
        kind: str,
        payload: Dict[str, Any],
    ) -> None:
        """Map a persisted compensation record back to live SQL.

        Kinds (in LIFO order, matching registration):

        * ``postgres.drop_database`` → terminate sessions + ``DROP DATABASE IF EXISTS``
        * ``postgres.drop_role``     → ``DROP USER IF EXISTS``

        Orchestrator iterates compensations in reverse, so the DB is always
        dropped before the role that owns it.
        """
        if not HAS_PSYCOPG2:
            raise RuntimeError("psycopg2 is not installed")

        conn = self._get_admin_connection()
        conn.autocommit = True
        try:
            cursor = conn.cursor()
            try:
                if kind == "postgres.drop_database":
                    self._drop_database(cursor, payload["database"])
                elif kind == "postgres.drop_role":
                    self._drop_role(cursor, payload["username"])
                else:
                    super().replay_compensation(agent_id, kind, payload)
            finally:
                cursor.close()
        finally:
            conn.close()

    def _drop_database(self, cursor, db_name: str) -> None:
        cursor.execute(
            sql.SQL(
                """
                SELECT pg_terminate_backend(pg_stat_activity.pid)
                FROM pg_stat_activity
                WHERE pg_stat_activity.datname = %s
                AND pid <> pg_backend_pid()
                """
            ),
            [db_name],
        )
        cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(db_name)))

    def _drop_role(self, cursor, username: str) -> None:
        cursor.execute(sql.SQL("DROP USER IF EXISTS {}").format(sql.Identifier(username)))
