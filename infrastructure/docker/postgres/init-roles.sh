#!/bin/sh
# Runs once on first start of the local PostgreSQL container.
# aieos_owner: owns the database, runs migrations and tenant provisioning (bypasses RLS as owner).
#              CREATEDB is local-only, so integration tests can create throwaway databases.
# aieos_api:   runtime login role for API and workers; receives aieos_app after migrations (make migrate).
set -eu
psql -v ON_ERROR_STOP=1 --username postgres \
     -v owner_pw="$AIEOS_OWNER_PASSWORD" -v api_pw="$AIEOS_API_PASSWORD" <<'SQL'
CREATE ROLE aieos_owner LOGIN CREATEROLE CREATEDB PASSWORD :'owner_pw';
CREATE ROLE aieos_api LOGIN PASSWORD :'api_pw';
CREATE DATABASE aieos OWNER aieos_owner;
REVOKE ALL ON DATABASE aieos FROM PUBLIC;
GRANT CONNECT ON DATABASE aieos TO aieos_api;
\connect aieos
ALTER SCHEMA public OWNER TO aieos_owner;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SQL
