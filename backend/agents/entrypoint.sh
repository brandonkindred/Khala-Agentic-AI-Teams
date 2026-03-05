#!/bin/bash
set -e

# Source NVM for Node.js availability in subprocesses
export NVM_DIR="${NVM_DIR:-/usr/local/nvm}"
if [ -s "$NVM_DIR/nvm.sh" ]; then
    . "$NVM_DIR/nvm.sh"
    nvm use 22.12 2>/dev/null || true
fi

# Verify tools are available
echo "=== Strands Agents - Tool Verification ==="
node --version || echo "WARN: node not found"
npm --version || echo "WARN: npm not found"
ng version 2>/dev/null | head -1 || echo "WARN: ng (Angular CLI) not found"
git --version || echo "WARN: git not found"
docker --version || echo "WARN: docker not found"
python --version || echo "WARN: python not found"
psql --version || echo "WARN: psql not found"
nginx -v 2>&1 || echo "WARN: nginx not found"

# Create log directory if not exists
mkdir -p /var/log/supervisor /var/log/nginx

# ---------------------------------------------------------------------------
# PostgreSQL Initialization (skip when using external Postgres)
# ---------------------------------------------------------------------------
POSTGRES_HOST="${POSTGRES_HOST:-}"
SUPERVISORD_CONF="/app/supervisord.conf"

if [ -n "$POSTGRES_HOST" ]; then
    echo "=== Using external PostgreSQL at $POSTGRES_HOST (skipping in-container Postgres) ==="
    SUPERVISORD_CONF="/app/supervisord-external-postgres.conf"
else
    echo "=== Initializing PostgreSQL ==="
    PGDATA="/var/lib/postgresql/data"
    POSTGRES_USER="${POSTGRES_USER:-strands}"
    POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-strands}"
    POSTGRES_DB="${POSTGRES_DB:-strands}"

    # Ensure directories exist with correct permissions
    mkdir -p "$PGDATA" /run/postgresql
    chown -R postgres:postgres "$PGDATA" /run/postgresql
    chmod 700 "$PGDATA"

    # Initialize database if not already initialized
    if [ ! -f "$PGDATA/PG_VERSION" ]; then
        echo "Initializing PostgreSQL database cluster..."
        su - postgres -c "/usr/lib/postgresql/15/bin/initdb -D $PGDATA --encoding=UTF8 --locale=C"
        
        # Configure PostgreSQL to accept local connections
        echo "host all all 0.0.0.0/0 md5" >> "$PGDATA/pg_hba.conf"
        echo "local all all trust" >> "$PGDATA/pg_hba.conf"
        
        # Configure PostgreSQL to listen on all interfaces
        echo "listen_addresses = '*'" >> "$PGDATA/postgresql.conf"
        echo "port = 5432" >> "$PGDATA/postgresql.conf"
        
        # Start PostgreSQL temporarily to create user and database
        echo "Starting PostgreSQL temporarily for initial setup..."
        su - postgres -c "/usr/lib/postgresql/15/bin/pg_ctl -D $PGDATA -w start"
        
        # Create user and database
        echo "Creating PostgreSQL user and database..."
        su - postgres -c "psql -c \"CREATE USER $POSTGRES_USER WITH PASSWORD '$POSTGRES_PASSWORD' SUPERUSER;\""
        su - postgres -c "psql -c \"CREATE DATABASE $POSTGRES_DB OWNER $POSTGRES_USER;\""
        
        # Stop PostgreSQL (supervisord will start it properly)
        echo "Stopping PostgreSQL temporary instance..."
        su - postgres -c "/usr/lib/postgresql/15/bin/pg_ctl -D $PGDATA -w stop"
        
        echo "PostgreSQL initialization complete."
    else
        echo "PostgreSQL data directory already exists, skipping initialization."
    fi
fi

# ---------------------------------------------------------------------------
# Start Services
# ---------------------------------------------------------------------------
echo "=== Starting Docker daemon, nginx, and API servers ==="
exec supervisord -c "$SUPERVISORD_CONF"
