-- Per-sandbox Postgres init script.
--
-- The compose stack runs a single Postgres instance for both agent app
-- data ("khala_sandbox", created by POSTGRES_DB) and Temporal persistence
-- ("temporal" + "temporal_visibility"). Owned by the same superuser
-- ("sandbox") since isolation is per-sandbox at the network level —
-- there's no second tenant inside a sandbox to defend against.

CREATE DATABASE temporal OWNER sandbox;
CREATE DATABASE temporal_visibility OWNER sandbox;
