# `strands_jobs` database (existing Postgres volumes)

The init script [`init/01-create-databases.sh`](01-create-databases.sh) creates the `strands_jobs` user and database **only on first cluster init**. If your Docker volume already existed before that script was extended, run once as a superuser (e.g. connect to the default `postgres` database):

```sql
CREATE USER strands_jobs WITH PASSWORD 'strands_jobs';
CREATE DATABASE strands_jobs OWNER strands_jobs;
```

Adjust the password to match `DATABASE_URL` / `docker/.env` if you change it from the default.
