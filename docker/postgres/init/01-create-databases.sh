#!/bin/bash
set -e
# Create temporal and khala databases and users for the stack.
# Runs as postgres superuser during first Postgres init.

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
  CREATE USER temporal WITH PASSWORD 'temporal';
  CREATE DATABASE temporal OWNER temporal;
  CREATE DATABASE temporal_visibility OWNER temporal;

  CREATE USER khala WITH PASSWORD 'khala';
  CREATE DATABASE khala OWNER khala;
  CREATE DATABASE khala_jobs OWNER khala;
EOSQL
