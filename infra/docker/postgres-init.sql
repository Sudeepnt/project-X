-- Runs once when the database volume is first created.
-- The app connects as a NON-superuser: superusers bypass row-level security, which isolates businesses.
CREATE ROLE projectx LOGIN PASSWORD 'projectx' NOSUPERUSER NOBYPASSRLS;
CREATE DATABASE projectx OWNER projectx;
\connect projectx
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
