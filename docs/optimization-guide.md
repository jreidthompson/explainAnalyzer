# Optimization guide

How to get from "the analyzer flagged something" to "the query is fast" with the least effort.
Everything the tool prints in its **Action plan** comes from this guide; sources are listed at the end.

## 1. The loop (about five commands)

```
# 1. capture - ANALYZE+BUFFERS drive the rules; VERBOSE makes every table schema.table; SETTINGS records work_mem etc.
psql -X -At -d DB -c "EXPLAIN (ANALYZE, BUFFERS, VERBOSE, SETTINGS, FORMAT JSON) <query>" > plan.json
# 2. analyze: findings + a ranked, copy-pasteable Action plan
python -m explain_analyzer plan.json --html report.html
# 3. let the tool test every action for you (each runs inside BEGIN ... ROLLBACK)
python -m explain_analyzer plan.json --script exp --query query.sql
(cd exp && psql -X -d DB -f experiments.sql)
# 4. see which action actually helped, and the SQL to make it permanent
python -m explain_analyzer --compare exp
# 5. apply the winner, then go back to step 1: the next bottleneck is now the top finding
```

**Why this order.** Refresh statistics first (cheap, and a correct estimate often removes later findings), then
structural changes (indexes, extended statistics, rewrites), then settings. Every change can alter the plan, so
re-capture between stages instead of applying everything the first report lists.

**Measuring honestly.** `EXPLAIN ANALYZE` executes the query. The generated script runs a warm-up first and
discards it, because a cold-cache baseline makes every later experiment look faster than it is. Each number is a single
execution: treat differences under ~10% as noise and repeat before believing a small win.

**Safety.** `EXPLAIN ANALYZE` on INSERT/UPDATE/DELETE/MERGE really modifies data - the script wraps every experiment in
a transaction it rolls back. Plain `CREATE INDEX` inside an experiment blocks writes to the table while it builds, so
run experiments on staging or a replica copy, not on a busy primary. `VACUUM` cannot run in a transaction, so
vacuum steps are marked NOT ROLLED BACK (routine maintenance).

## 2. Finding -> cause -> fix -> how to test

| Finding | Usual cause | Fix (action plan emits the SQL) | Test it |
|---|---|---|---|
| Row estimate off Nx | stale stats; correlated columns; expression/function on a column; skewed join keys | `ANALYZE`; `ALTER TABLE .. SET STATISTICS`; `CREATE STATISTICS` (multi-column / expression) | experiment, then check the node's estimate vs actual |
| Seq scan discards most rows | no index on the filtered column(s) | btree index: equality columns first, one range column last | `CREATE INDEX` in the rolled-back experiment |
| Seq scan repeated N times | correlated sub-select (SubPlan) or nested loop probing an unindexed column | index the correlated column, or rewrite as a join with GROUP BY / LATERAL | index experiment; rewrite by hand |
| Rows Removed by Filter on an index scan | filter column is not in the index | extend the index with the filter column(s) | index experiment |
| Rows Removed by Index Recheck | lossy bitmap (work_mem too small) | raise `work_mem` for the query | `SET LOCAL work_mem` |
| Sort spilled to disk | `work_mem` too small for the sort | `SET LOCAL work_mem`; or an index that supplies the order | variants at 2x/4x/8x the spill size |
| Hash split into batches (planned) | build side **over-estimated** by the planner | fix the estimate (see first row) | ANALYZE / statistics experiment; Hash should show `Batches: 1` |
| Hash split into batches (grew) | hash table really outgrew memory | `hash_mem_multiplier` / `work_mem` | `SET LOCAL` variants |
| Nested loop, huge inner loop count | outer side under-estimated; or inner join column unindexed | fix outer estimate; index inner join column | experiment; `enable_nestloop = off` as a *diagnostic only* |
| Index-only scan with Heap Fetches | stale visibility map | `VACUUM (ANALYZE) table`; tune per-table autovacuum | run VACUUM, re-capture |
| Lots of shared reads | cold cache, bloat, non-selective index, poor locality | re-run warm; VACUUM / REINDEX CONCURRENTLY / pg_repack; covering index | compare blocks per returned row |
| Workers launched < planned | worker pool exhausted by concurrent load | check `max_parallel_workers`, `max_worker_processes` | run when the system is quiet |
| JIT cost is a big share | JIT compile time on a short query | `SET jit = off` or raise `jit_above_cost` | `SET LOCAL jit = off` |
| Planning time ~ execution time | many partitions / joins; replanning every call | prepared statements; prune partitions; `join_collapse_limit` | repeat the prepared statement |
| Trigger time | FK check on an unindexed referencing column | index the FK columns of the referencing table | re-run the DML |

## 3. Rules of thumb behind the actions

**Indexes.** For a multicolumn btree, equality constraints on leading columns plus an inequality on the first column
without an equality constraint limit the scanned portion; columns after that are only filtered inside the index. So
`WHERE status = 3 AND dow BETWEEN 10 AND 200` wants `(status, dow)`. More than three columns rarely pays off. A column
whose filter keeps ~half the table is not worth indexing (the tool will not suggest it). Special cases:
a function or cast on the column needs an **expression index** (`lower(email)`) or a rewritten predicate;
`LIKE '%x%'` needs **pg_trgm** (`USING gin (col gin_trgm_ops)`); `LIKE 'x%'` needs `text_pattern_ops` unless the column uses
the C collation; an `OR` across columns cannot use one btree range (index each side, or `UNION ALL`);
a partial index (`WHERE deleted_at IS NULL`) is smaller and cheaper to maintain but only matches queries whose WHERE
implies its predicate (not parameterised ones). Index-only scans need all queried columns in the index
(`INCLUDE` for payload columns) **and** a fresh visibility map (VACUUM). Every index slows writes - drop ones whose
`pg_stat_user_indexes.idx_scan` stays 0.
`CREATE INDEX CONCURRENTLY` avoids blocking writes but cannot run inside a transaction and leaves an INVALID index if it
fails (drop it and retry).

**Statistics.** Order of cheapness: `ANALYZE` -> raise the per-column target (`ALTER TABLE .. ALTER COLUMN .. SET
STATISTICS 1000`, default 100) -> `CREATE STATISTICS`. Extended statistics: *dependencies* (equality/IN on dependent
columns), *ndistinct* (GROUP BY on several columns), *mcv* (frequent combinations; also ranges); omitting the kind list
builds all kinds; **run ANALYZE afterwards**; they describe columns of one table and do not repair join-size errors.
Autovacuum does not analyze partitioned parent tables or foreign tables - do those by hand.

**Memory.** `work_mem` (default 4MB) is the limit *per sort/hash node, per backend, per parallel worker*, so total
memory is a multiple of it; use `SET LOCAL` for one query rather than a global change. Hash nodes may use
`work_mem x hash_mem_multiplier` (default 2.0). The in-memory size of a sort is larger than its on-disk spill: in our
calibration run a 21 MB spill needed ~34 MB (1.6x) for 70-byte rows, so the tool tries 2x, 4x and 8x the spill size and
you keep the smallest that shows `quicksort`. `EXPLAIN (SETTINGS)` lists only settings that differ from the default -
a missing `hash_mem_multiplier` means the default applied.

**The planner's `enable_*` switches are diagnostics.** `SET enable_nestloop = off` tells you whether the plan *could* be
faster; it does not tell you why the planner chose badly. Fix the cause (estimate or index). Cost constants are a last
resort and apply to your whole workload: `random_page_cost` 4.0 is tuned for spinning disks, SSD-backed systems
commonly use 1.1-2.0; `effective_cache_size` should reflect the cache actually available (it allocates nothing).

**Maintenance.** `VACUUM` keeps the visibility map fresh (index-only scans) and bloat down. Autovacuum fires after
`threshold + scale_factor x rows` changes, so big tables need smaller per-table scale factors
(`ALTER TABLE t SET (autovacuum_vacuum_scale_factor = 0.02, autovacuum_vacuum_insert_scale_factor = 0.05)`).
Bloated indexes: `REINDEX INDEX CONCURRENTLY` (PG12+); bloated tables: pg_repack / pg_squeeze (concurrent) or CLUSTER
(blocks). Watch **blocks per returned row**: a node reading far more 8 kB blocks than it returns rows has an efficiency
problem even if it is not the slowest node today.

**Other levers.** CTEs: inlined when referenced once (PG12+), materialized when referenced more than once; force with
`AS MATERIALIZED` / `AS NOT MATERIALIZED`. Set-returning functions default to `ROWS 1000` / `COST 100`
(`ALTER FUNCTION f(..) ROWS n COST c`), a frequent source of wrong estimates. Partition pruning needs conditions on the
partition key compared with constants/parameters (not volatile expressions); look for `Subplans Removed`. Prepared
statements use custom plans for the first five executions, then a generic plan if its cost is comparable
(`plan_cache_mode`). Parallel plans are not used for writes and require parallel-safe functions.

## 4. Testing an index without building it: HypoPG

`CREATE EXTENSION hypopg; SELECT hypopg_create_index('CREATE INDEX ON t (a, b)');` then plain `EXPLAIN` (not
`EXPLAIN ANALYZE`) shows whether the planner *would* use it, at no build cost. Hypothetical indexes live in one
session and `EXPLAIN ANALYZE` ignores them, so use HypoPG to shortlist candidates and the generated experiments to
measure real time.

## 5. Capturing good plans

* Required: `ANALYZE`, `BUFFERS`. Recommended: `VERBOSE`, `SETTINGS`, `FORMAT JSON`. `WAL`/`MEMORY` are harmless extras.
* **`VERBOSE` is how you make the plan declare `schema.table`.** Without it `EXPLAIN` leaves the schema off every table
  that is on the capturing session's `search_path` (JSON has no `"Schema"` key at all), so generated SQL and the
  experiment script cannot tell same-named tables in different schemas apart. It also adds per-node `Output:` column
  lists, which make plans of wide rows large; the analyzer ignores them. If you cannot use `VERBOSE`, pass `--schema NAME`.
* `track_io_timing = on` adds I/O timings to the BUFFERS output (it costs clock calls; test with `pg_test_timing`).
* `TIMING OFF` reduces overhead on very large plans but removes the per-node times the time-based rules use.
* A psql `.psqlrc` that prints `SET` lines before the plan is tolerated; `psql -X` skips it.

## Sources

PostgreSQL documentation: [Using EXPLAIN](https://www.postgresql.org/docs/current/using-explain.html),
[EXPLAIN](https://www.postgresql.org/docs/current/sql-explain.html),
[Planner statistics](https://www.postgresql.org/docs/current/planner-stats.html),
[CREATE STATISTICS](https://www.postgresql.org/docs/current/sql-createstatistics.html),
[CREATE INDEX](https://www.postgresql.org/docs/current/sql-createindex.html),
[Multicolumn indexes](https://www.postgresql.org/docs/current/indexes-multicolumn.html),
[Index-only scans and covering indexes](https://www.postgresql.org/docs/current/indexes-index-only-scans.html),
[Partial indexes](https://www.postgresql.org/docs/current/indexes-partial.html),
[Expression indexes](https://www.postgresql.org/docs/current/indexes-expressional.html),
[pg_trgm](https://www.postgresql.org/docs/current/pgtrgm.html),
[Resource consumption (work_mem, hash_mem_multiplier)](https://www.postgresql.org/docs/current/runtime-config-resource.html),
[Query planning settings](https://www.postgresql.org/docs/current/runtime-config-query.html),
[Routine vacuuming](https://www.postgresql.org/docs/current/routine-vacuuming.html),
[Statistics collector settings (track_io_timing)](https://www.postgresql.org/docs/current/runtime-config-statistics.html),
[Parallel plans](https://www.postgresql.org/docs/current/parallel-plans.html),
[Table partitioning (pruning)](https://www.postgresql.org/docs/current/ddl-partitioning.html),
[WITH queries (CTE materialization)](https://www.postgresql.org/docs/current/queries-with.html),
[CREATE FUNCTION (COST/ROWS)](https://www.postgresql.org/docs/current/sql-createfunction.html),
[PREPARE](https://www.postgresql.org/docs/current/sql-prepare.html).
Practitioner guidance: PostgreSQL wiki [Slow Query Questions](https://wiki.postgresql.org/wiki/Slow_Query_Questions);
pgMustard blog: [Why isn't Postgres using my index?](https://www.pgmustard.com/blog/why-isnt-postgres-using-my-index),
[Indexing best practices](https://www.pgmustard.com/blog/indexing-best-practices-postgresql),
[Read efficiency issues](https://www.pgmustard.com/blog/read-efficiency-issues-in-postgres-queries),
[Row count estimates](https://www.pgmustard.com/blog/row-count-estimates-in-postgres),
[Using BUFFERS](https://www.pgmustard.com/blog/using-postgres-buffers-for-query-optimization);
[HypoPG](https://github.com/HypoPG/hypopg); [PEV2](https://github.com/dalibo/pev2).
Measured on PostgreSQL 17 during development: sort spill-to-memory ratio (1.6x), ANALYZE / CREATE STATISTICS /
CREATE INDEX inside BEGIN..ROLLBACK experiments, and an 18.8 s -> 27 ms correlated-subquery fix.
