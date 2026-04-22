#!/bin/bash
set -e
# Per-team sandbox Postgres roles and databases (issue #257).
#
# Creates one (role, database) pair per currently-wired team so that sandboxes
# for agents in team X receive credentials that can only reach team X's data.
# Passwords come from POSTGRES_PASSWORD_SANDBOX_<TEAM> env vars plumbed into
# the postgres service via docker-compose.yml. If a var is unset we skip that
# team with a warning — the provisioner falls back to the global POSTGRES_*
# creds in that case, keeping local dev working without per-team setup.
#
# Verification (after the stack is up):
#   docker exec khala-stack-postgres psql -U postgres -c '\du' | grep sandbox_
#   docker exec khala-stack-postgres psql -U postgres -c '\l'  | grep sandbox_
#   docker exec -e PGPASSWORD=$POSTGRES_PASSWORD_SANDBOX_BLOGGING \
#     khala-stack-postgres psql -U sandbox_blogging -d sandbox_software_engineering -c '\dt'
#   # Expected: permission denied to connect to database "sandbox_software_engineering"

create_team_role() {
  local team="$1"
  local password_var="POSTGRES_PASSWORD_SANDBOX_${team^^}"
  local password="${!password_var:-}"
  if [ -z "$password" ]; then
    echo "WARN: $password_var is unset; skipping sandbox_${team} role/database" >&2
    return 0
  fi
  # Escape single quotes for the SQL string literal — passwords with special
  # characters would otherwise break the CREATE USER statement.
  local escaped
  escaped=$(printf '%s' "$password" | sed "s/'/''/g")
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE USER "sandbox_${team}" WITH PASSWORD '${escaped}';
CREATE DATABASE "sandbox_${team}" OWNER "sandbox_${team}";
REVOKE ALL ON DATABASE "sandbox_${team}" FROM PUBLIC;
GRANT CONNECT ON DATABASE "sandbox_${team}" TO "sandbox_${team}";
SQL
}

for team in blogging software_engineering planning_v3 branding; do
  create_team_role "$team"
done
