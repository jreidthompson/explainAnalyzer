# explain-analyzer

Offline analyzer for PostgreSQL `EXPLAIN` plans. Pure Python standard library (**3.12+**), no network
access, no telemetry - safe for plans that reference proprietary schema or data. It reproduces the
useful parts of explain.depesz.com, Dalibo PEV2, pgMustard and explain.tensor.ru: per-node
*exclusive* time, row-estimate error, buffer analysis and ranked, explained findings.

## Fastest path: from slow query to a tested fix
```
psql -X -At -d DB -c "EXPLAIN (ANALYZE, BUFFERS, SETTINGS, FORMAT JSON) <your query>" > plan.json
python -m explain_analyzer plan.json --html report.html      # findings + ranked Action plan with copy-paste SQL
python -m explain_analyzer plan.json --script exp --query query.sql
(cd exp && psql -X -d DB -f experiments.sql)                  # tries every action inside BEGIN..ROLLBACK
python -m explain_analyzer --compare exp                      # which one helped, and the SQL to apply it
```
The **Action plan** turns each finding into staged, ready-to-run SQL (ANALYZE, `CREATE INDEX` with the right column
order, `CREATE STATISTICS`, `SET LOCAL work_mem` sized from the observed spill, `VACUUM`, rewrite templates), each with
a rollback-safe "try" version, a production "apply" version, cautions and what to check afterwards.
`--script` runs all of them for you; `--compare` ranks the results and prints the smallest change that captures
nearly all of the gain. Details and the reasoning behind each action: [docs/optimization-guide.md](docs/optimization-guide.md).

`ANALYZE` **executes** the statement (DML is rolled back by the generated script). Run experiments on staging or a
replica copy: a plain `CREATE INDEX` blocks writes to its table while building.

## Use
```
python -m explain_analyzer plan.json                 # ranked findings + Action plan + annotated tree
python -m explain_analyzer plan.txt --html out.html  # self-contained report (no external assets)
cat plan.json | python -m explain_analyzer --json    # machine-readable (findings, actions, nodes)
python -m explain_analyzer plan.json --fail-on critical   # exit 2 for CI gates
python -m explain_analyzer plan.json --sanitize names     # redact constants + hash identifiers before sharing
python -m explain_analyzer plan.json --set mis_warn=5 --set hot_warn_pct=15   # tune thresholds (see metrics.Config)
python -m explain_analyzer --compare before.json after.json   # did a change help? node-by-node deltas
```
Install as a command with `pip install .` (`explain-analyzer`). Run tests: `python -m unittest discover -s tests -t .`

## What it detects
hot nodes (exclusive time share) - row mis-estimates (reported at the node where they originate,
inherited ones as info; over-estimates under `LIMIT`/merge joins ignored) - seq scans that discard most rows
or repeat in loops - large Rows Removed by Filter / Join Filter / Index Recheck - sorts, hash joins and
hash aggregates spilling to disk - nested loops with huge inner loop counts - index-only scans with heap
fetches - cold-cache reads / I/O-bound nodes - parallel workers not launched - JIT overhead - planning
time - slow triggers - many partitions scanned - Memoize evictions. Plans without ANALYZE get cost-based
checks only.

## Notes and limits
* Exclusive time = node time x loops, divided by participants below Gather, minus child time. InitPlan/CTE
  time is deducted from the node that consumes it. It is an approximation, as in the web tools.
* `--sanitize` is best effort (regex based); review output before sharing outside your organisation.
* Index/statistics names and column lists are derived from the plan's conditions; always read the SQL before running it.
* The PEV2 graph view: download `pev2.html` from github.com/dalibo/pev2/releases once and open it locally
  (it runs fully offline). It is not bundled or automated here.
* Tests assert that no network modules are imported and that generated HTML references no external URLs.
