#!/usr/bin/env bash
# Capture an EXPLAIN plan as JSON via psql. Usage: capture.sh [psql args...] < query.sql > plan.json
# WARNING: ANALYZE executes the statement. For INSERT/UPDATE/DELETE the statement is wrapped
# in a transaction that is rolled back.
set -euo pipefail
q=$(cat)
psql -X -q -At "$@" <<SQL
BEGIN;
EXPLAIN (ANALYZE, BUFFERS, VERBOSE, SETTINGS, FORMAT JSON) ${q%;};   -- VERBOSE: schema-qualified table names
ROLLBACK;
SQL
