#!/bin/bash
set -e
# Create temporal and strands databases and users for the stack.
# Runs as postgres superuser during first Postgres init.

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
  CREATE USER temporal WITH PASSWORD 'temporal';
  CREATE DATABASE temporal OWNER temporal;
  CREATE DATABASE temporal_visibility OWNER temporal;

  CREATE USER strands WITH PASSWORD 'strands';
  CREATE DATABASE strands OWNER strands;
EOSQL
