-- Create the temporal and khala databases and roles for the stack.
-- Runs as the POSTGRES_USER superuser during first Postgres init. Pure SQL
-- (no shell script) so it can never be skipped due to a missing exec bit.

CREATE USER temporal WITH PASSWORD 'temporal';
CREATE DATABASE temporal OWNER temporal;
CREATE DATABASE temporal_visibility OWNER temporal;

CREATE USER khala WITH PASSWORD 'khala';
CREATE DATABASE khala OWNER khala;
CREATE DATABASE khala_jobs OWNER khala;
