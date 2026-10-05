"""Rule registry. Each rule inspects an analysed Plan and returns Findings."""
from __future__ import annotations

import re
from typing import Callable

from .context import chain, describe
from .metrics import BLOCK_KB, Config
from .model import Finding, Node, Plan

Rule = Callable[[Plan, Config], list[Finding]]
RULES: list[Rule] = []

INFO, WARN, CRIT = 1, 2, 3


def rule(fn: Rule) -> Rule:
    RULES.append(fn)
    return fn


def _where(n: Node) -> str:
    return f"#{n.id} {n.label()}"


def _cols(expr: str | None) -> str:
    return "the columns used in the filter"


def _fmt_rows(x: float) -> str:
    return f"{x:,.0f}"


def _fmt_kb(kb: float) -> str:
    return f"{kb / 1024:.1f} MB" if kb >= 1024 else f"{kb:.0f} kB"


def _sev(value: float, warn: float, crit: float, info: float | None = None) -> int:
    if value >= crit:
        return CRIT
    if value >= warn:
        return WARN
    if info is not None and value >= info:
        return INFO
    return 0


# ------------------------------------------------------------------ rules

@rule
def no_analyze(plan: Plan, cfg: Config) -> list[Finding]:
    if plan.m["analyzed"]:
        return []
    out = [Finding(
        "no-analyze", INFO, "Plan has no ANALYZE data",
        "Only estimated costs are available; time, row-estimate and buffer rules were skipped.",
        "Re-run with EXPLAIN (ANALYZE, BUFFERS) on a representative dataset "
        "(wrap DML in BEGIN ... ROLLBACK).")]
    for n in plan.nodes:
        if n.m["cost_pct"] >= cfg.cost_hot_pct and n.m["excl_cost"] > 0:
            out.append(Finding(
                "hot-cost", WARN, f"Node accounts for {n.m['cost_pct']:.0f}% of estimated cost",
                f"{_where(n)} exclusive cost {n.m['excl_cost']:,.1f} of {plan.root.get('Total Cost'):,.1f}.",
                "Check the access path and join order of this node.", n.id))
        if n.node_type == "Seq Scan" and n.get("Filter") and n.m["est_rows"] >= 100000:
            out.append(Finding(
                "seq-scan-estimate", INFO, "Large sequential scan with a filter",
                f"{_where(n)} is estimated to return {_fmt_rows(n.m['est_rows'])} rows after filtering: {n.get('Filter')}",
                "Consider an index covering the filter columns if the filter is selective.", n.id))
    return out


@rule
def hot_node(plan: Plan, cfg: Config) -> list[Finding]:
    out = []
    if not plan.m["analyzed"]:
        return out
    for n in plan.nodes:
        ms, pct = n.m["excl_ms"], n.m["excl_pct"]
        if ms < cfg.hot_min_ms:
            continue
        sev = _sev(pct, cfg.hot_warn_pct, cfg.hot_crit_pct, cfg.hot_info_pct)
        if sev:
            out.append(Finding(
                "hot-node", sev, f"{pct:.0f}% of execution time spent in this node",
                f"{_where(n)}: {ms:,.1f} ms exclusive ({n.m['loops']:,} loop(s)).",
                "Start tuning here; see the other findings for this node.", n.id, ms))
    return out


@rule
def row_misestimate(plan: Plan, cfg: Config) -> list[Finding]:
    out = []
    for n in plan.nodes:
        m = n.m
        if m["mis"] < cfg.mis_warn or m["mis_ignored"]:
            continue
        sev = CRIT if m["mis"] >= cfg.mis_crit else WARN
        under = m["mis_dir"] == "under"
        verb = "under-estimated" if under else "over-estimated"
        if not m["mis_origin"]:
            sev = INFO
        origin = "originates here" if m["mis_origin"] else "inherited from a child node"
        sug = ""
        if m["mis_origin"]:
            sug = ("Run ANALYZE on the tables involved; if columns are correlated, add extended "
                   "statistics (CREATE STATISTICS ... (dependencies, ndistinct, mcv)); avoid functions/casts "
                   "on filtered columns; consider raising default_statistics_target for skewed columns.")
        cond = n.get("Filter") or n.get("Index Cond") or n.get("Hash Cond") or n.get("Merge Cond") or n.get("Join Filter")
        det = (f"{_where(n)}: planner expected {_fmt_rows(m['est_rows'])} row(s)/loop, got "
               f"{_fmt_rows(m['act_rows'])} ({verb} {m['mis']:,.0f}x, {origin})."
               + (f" Condition: {cond}" if cond and m["mis_origin"] else ""))
        # Under-estimates are what push the planner towards nested loops.
        impact = m["excl_ms"] if m["mis_origin"] else 0.0
        out.append(Finding("row-misestimate", sev, f"Row estimate off by {m['mis']:,.0f}x ({verb})",
                           det, sug, n.id, impact))
    return out


@rule
def seq_scan_filter(plan: Plan, cfg: Config) -> list[Finding]:
    out = []
    if not plan.m["analyzed"]:
        return out
    for n in plan.nodes:
        if n.node_type != "Seq Scan" or n.m["never"]:
            continue
        removed = float(n.get("Rows Removed by Filter") or 0) * n.m["loops"]
        kept = n.m["rows_total"]
        if removed < cfg.filter_min_removed or removed / max(removed + kept, 1) < cfg.filter_ratio:
            continue
        pct = removed / max(removed + kept, 1) * 100
        sev = CRIT if n.m["excl_pct"] >= cfg.hot_warn_pct else \
            WARN if n.m["excl_pct"] >= cfg.hot_info_pct else INFO
        out.append(Finding(
            "seq-scan-filter", sev, "Sequential scan discards most rows",
            f"{_where(n)} read {_fmt_rows(removed + kept)} rows and kept {_fmt_rows(kept)} ({pct:.1f}% discarded). "
            f"Filter: {n.get('Filter')}",
            f"Add an index on {_cols(n.get('Filter'))} (or a partial index matching the predicate). "
            "If an index exists, check that the predicate is sargable (no function/cast on the column) "
            "and that statistics are fresh.", n.id, n.m["excl_ms"]))
    return out


@rule
def seq_scan_loops(plan: Plan, cfg: Config) -> list[Finding]:
    out = []
    for n in plan.nodes:
        if n.node_type == "Seq Scan" and n.m["loops"] >= 100 and plan.m["analyzed"] and \
                n.m["excl_pct"] >= cfg.hot_info_pct:
            out.append(Finding(
                "seq-scan-loops", CRIT, f"Sequential scan repeated {n.m['loops']:,} times",
                f"{_where(n)} is the inner side of a nested loop / subplan and costs {n.m['excl_ms']:,.1f} ms in total.",
                "Index the join/lookup column on this table, or make the planner choose a hash/merge join "
                "(fix the outer row estimate).", n.id, n.m["excl_ms"]))
    return out


@rule
def rows_removed(plan: Plan, cfg: Config) -> list[Finding]:
    out = []
    if not plan.m["analyzed"]:
        return out
    for n in plan.nodes:
        if n.m["never"] or n.node_type == "Seq Scan":
            continue
        kept = max(n.m["rows_total"], 1)
        for key, what, adv in (
            ("Rows Removed by Filter", "filter",
             "Include the filtered columns in the index (or use a partial/composite index) so rows are rejected inside the index scan."),
            ("Rows Removed by Join Filter", "join filter",
             "The join condition is applied after pairing rows; add an equality condition or index so fewer pairs are produced."),
            ("Rows Removed by Index Recheck", "index recheck",
             "Bitmap is lossy: raise work_mem so the bitmap stays exact, or use a more selective index."),
        ):
            removed = float(n.get(key) or 0) * n.m["loops"]
            if removed >= cfg.filter_min_removed and removed >= 10 * kept:
                out.append(Finding(
                    "rows-removed", WARN, f"{key}: {_fmt_rows(removed)} rows discarded",
                    f"{_where(n)} discarded {_fmt_rows(removed)} rows in its {what} and returned {_fmt_rows(kept)}.",
                    adv, n.id, n.m["excl_ms"]))
    return out


@rule
def spills(plan: Plan, cfg: Config) -> list[Finding]:
    out = []
    for n in plan.nodes:
        if n.get("Sort Space Type") == "Disk":
            kb = n.get("Sort Space Used") or 0
            out.append(Finding(
                "sort-spill", WARN if n.m["excl_pct"] < cfg.hot_warn_pct else CRIT,
                f"Sort spilled to disk ({_fmt_kb(kb)})",
                f"{_where(n)} used {n.get('Sort Method')} with {_fmt_kb(kb)} on disk. Sort Key: {n.get('Sort Key')}",
                f"Raise work_mem for this query (SET LOCAL work_mem) - the in-memory footprint is larger than the "
                f"on-disk figure, so try a few times {_fmt_kb(kb)}; or add an index that provides the order.",
                n.id, n.m["excl_ms"]))
        batches = n.get("Hash Batches")
        if batches and batches > 1:
            out.append(Finding(
                "hash-spill", WARN, f"Hash join split into {batches} batches",
                f"{_where(n)} exceeded work_mem (peak {_fmt_kb(n.get('Peak Memory Usage') or 0)}), "
                "so both sides were written to temp files.",
                "Raise work_mem / hash_mem_multiplier, or reduce the build side (better row estimate, "
                "filter earlier, narrower columns).", n.id, n.m["excl_ms"]))
        du = n.get("Disk Usage")
        if du:
            out.append(Finding(
                "agg-spill", WARN, f"Hash aggregate spilled to disk ({_fmt_kb(du)})",
                f"{_where(n)} used {n.get('HashAgg Batches')} batch(es), peak memory {_fmt_kb(n.get('Peak Memory Usage') or 0)}.",
                "Raise work_mem / hash_mem_multiplier, or check the group-count estimate.", n.id, n.m["excl_ms"]))
        tw = (n.get("Temp Written Blocks") or 0)
        if tw and n.node_type not in ("Sort", "Hash", "Aggregate", "Incremental Sort") and \
                n.m["buf_excl"].get("Temp Written Blocks", 0) > 0:
            kb = n.m["buf_excl"]["Temp Written Blocks"] * BLOCK_KB
            out.append(Finding(
                "temp-files", INFO, f"Temp files written ({_fmt_kb(kb)})",
                f"{_where(n)} wrote temporary blocks (e.g. Materialize, CTE, or window function spilling).",
                "Check work_mem and whether the intermediate result can be reduced.", n.id, n.m["excl_ms"]))
    return out


@rule
def nested_loop(plan: Plan, cfg: Config) -> list[Finding]:
    out = []
    if not plan.m["analyzed"]:
        return out
    for n in plan.nodes:
        if n.node_type != "Nested Loop":
            continue
        sides = [c for c in n.children if not c.is_subplan_child()]
        if len(sides) < 2:
            continue
        outer, inner = sides[0], sides[1]
        loops = inner.m["loops"]
        if loops < cfg.nl_loops:
            continue
        inner_ms = inner.m["incl_ms"] or 0.0
        pct = inner_ms / plan.m["total_ms"] * 100 if plan.m["total_ms"] else 0
        if pct < cfg.nl_inner_pct:
            continue
        sev = WARN
        why = ""
        if outer.m["mis_dir"] == "under" and outer.m["mis"] >= cfg.mis_warn:
            sev = CRIT
            why = (f" The outer side returned {outer.m['mis']:,.0f}x more rows than estimated, "
                   "which is what made the planner choose a nested loop.")
        out.append(Finding(
            "nested-loop", sev, f"Nested loop runs its inner side {loops:,} times",
            f"{_where(n)}: inner side #{inner.id} ({chain(inner)}) takes {inner_ms:,.1f} ms total "
            f"({pct:.0f}% of runtime) across {loops:,} executions.{why}",
            "Ensure the inner join column is indexed, fix the outer row estimate (ANALYZE / extended statistics), "
            "or test with SET enable_nestloop = off to see whether a hash/merge join is faster.", n.id, inner_ms))
    return out


@rule
def heap_fetches(plan: Plan, cfg: Config) -> list[Finding]:
    out = []
    for n in plan.nodes:
        hf = n.get("Heap Fetches")
        if n.node_type != "Index Only Scan" or not hf or not plan.m["analyzed"]:
            continue
        rows = max(n.m["rows_total"], 1)
        if hf >= cfg.heap_fetch_min and hf / rows >= cfg.heap_fetch_ratio:
            out.append(Finding(
                "heap-fetches", WARN, f"Index-only scan still visits the heap ({_fmt_rows(hf)} fetches)",
                f"{_where(n)}: {hf / rows * 100:.0f}% of returned rows needed a heap visit because the visibility map is stale.",
                "VACUUM the table (and tune autovacuum_vacuum_scale_factor / insert thresholds for it).",
                n.id, n.m["excl_ms"]))
    return out


@rule
def io(plan: Plan, cfg: Config) -> list[Finding]:
    out = []
    if not plan.m["analyzed"] or not plan.m["has_buffers"]:
        return out
    for n in plan.nodes:
        read = n.m["buf_excl"].get("Shared Read Blocks", 0)
        ratio = n.m["hit_ratio"]
        if read >= cfg.read_min_blocks and ratio is not None and (1 - ratio) >= cfg.read_ratio:
            out.append(Finding(
                "disk-reads", WARN, f"Read {_fmt_kb(read * BLOCK_KB)} from outside shared_buffers",
                f"{_where(n)}: {read:,} blocks read, cache hit ratio {ratio * 100:.0f}%. "
                "Reads may have come from the OS cache or disk (see I/O Timings if track_io_timing is on).",
                "Likely a cold cache: re-run to see warm behaviour. If it stays slow, reduce the data touched "
                "(better index, narrower rows, partitioning) or increase shared_buffers / RAM.",
                n.id, n.m["excl_ms"]))
        io_ms = n.m["io_read_ms"]
        if io_ms and n.m["excl_ms"] and io_ms / n.m["excl_ms"] >= 0.5 and io_ms >= 5:
            out.append(Finding(
                "io-bound", INFO, f"{io_ms / n.m['excl_ms'] * 100:.0f}% of node time is read I/O",
                f"{_where(n)} spent {io_ms:,.1f} ms waiting on reads out of {n.m['excl_ms']:,.1f} ms.",
                "Storage latency dominates this node; reduce blocks touched or warm the cache.", n.id, io_ms))
    tot = plan.m["buf_total"]
    hit, rd = tot.get("Shared Hit Blocks", 0), tot.get("Shared Read Blocks", 0)
    if rd >= cfg.read_min_blocks and hit / max(hit + rd, 1) < 0.9:
        out.append(Finding(
            "cache-hit-ratio", INFO, f"Overall shared-buffer hit ratio {hit / (hit + rd) * 100:.0f}%",
            f"{rd:,} blocks ({_fmt_kb(rd * BLOCK_KB)}) read versus {hit:,} hits.",
            "A single cold run is not representative; repeat the EXPLAIN to compare.", None))
    return out


@rule
def parallel(plan: Plan, cfg: Config) -> list[Finding]:
    out = []
    for n in plan.nodes:
        pl, la = n.get("Workers Planned"), n.get("Workers Launched")
        if pl is not None and la is not None and la < pl:
            out.append(Finding(
                "workers-launched", WARN, f"Only {la} of {pl} parallel workers launched",
                f"{_where(n)} could not start all planned workers.",
                "Check max_worker_processes, max_parallel_workers and concurrent load.", n.id))
    return out


@rule
def jit(plan: Plan, cfg: Config) -> list[Finding]:
    if not plan.jit or not plan.execution_time:
        return []
    total = (plan.jit.get("Timing") or {}).get("Total")
    if total is None:
        return []
    pct = total / plan.execution_time * 100
    sev = _sev(pct, cfg.jit_warn_pct, cfg.jit_crit_pct)
    if not sev:
        return []
    return [Finding(
        "jit", sev, f"JIT compilation took {total:,.1f} ms ({pct:.0f}% of execution)",
        f"{plan.jit.get('Functions')} function(s); timing {plan.jit.get('Timing')}.",
        "For short OLTP-style queries SET jit = off, or raise jit_above_cost / jit_inline_above_cost / "
        "jit_optimize_above_cost. A JIT-heavy plan often signals an over-estimated cost.", None, total)]


@rule
def planning(plan: Plan, cfg: Config) -> list[Finding]:
    pt, et = plan.planning_time, plan.execution_time
    if pt is None or et is None or pt < cfg.planning_min_ms:
        return []
    pct = pt / max(et, 0.001) * 100
    if pct < cfg.planning_warn_pct:
        return []
    return [Finding(
        "planning-time", WARN, f"Planning took {pt:,.1f} ms ({pct:.0f}% of execution time)",
        "Planning cost is comparable to execution.",
        "Use prepared statements / plan caching, reduce partition count or join-tree size "
        "(join_collapse_limit, from_collapse_limit), or check for catalog bloat.", None, pt)]


@rule
def triggers(plan: Plan, cfg: Config) -> list[Finding]:
    out = []
    total = plan.execution_time or plan.m.get("total_ms") or 0
    for t in plan.triggers:
        ms = float(t.get("Time") or 0)
        if total and ms / total * 100 >= cfg.trigger_pct and ms >= 1:
            out.append(Finding(
                "trigger", WARN, f"Trigger {t.get('Trigger Name')} took {ms:,.1f} ms",
                f"{t.get('Calls')} call(s); {ms / total * 100:.0f}% of execution time.",
                "For FK triggers make sure the referencing column is indexed; "
                "otherwise review the trigger function.", None, ms))
    return out


@rule
def partitions(plan: Plan, cfg: Config) -> list[Finding]:
    out = []
    for n in plan.nodes:
        if n.node_type in ("Append", "Merge Append"):
            scanned = [c for c in n.children if not c.m["never"]]
            if len(scanned) >= cfg.many_partitions:
                out.append(Finding(
                    "many-partitions", WARN, f"{len(scanned)} child scans under {n.node_type}",
                    f"{_where(n)} scans {len(scanned)} partitions/branches "
                    f"({n.get('Subplans Removed') or 0} removed by runtime pruning).",
                    "Filter on the partition key with constants/parameters so partitions are pruned, "
                    "or reduce partition count.", n.id, n.m["excl_ms"]))
    return out


@rule
def index_searches(plan: Plan, cfg: Config) -> list[Finding]:
    out = []
    for n in plan.nodes:
        s = n.get("Index Searches")
        if s and s > 100 and s / max(n.m["loops"], 1) >= 100 and n.m["excl_pct"] >= cfg.hot_info_pct:
            out.append(Finding(
                "index-searches", INFO, f"{_fmt_rows(s)} index descents for one scan",
                f"{_where(n)} walked the index {_fmt_rows(s)} times (e.g. big IN-list/array or skip scan).",
                "Check whether a different index column order or a join would touch the index fewer times.",
                n.id, n.m["excl_ms"]))
    return out


@rule
def memoize(plan: Plan, cfg: Config) -> list[Finding]:
    out = []
    for n in plan.nodes:
        if n.node_type == "Memoize" and (n.get("Cache Evictions") or n.get("Cache Overflows")):
            out.append(Finding(
                "memoize-evictions", INFO, "Memoize cache is evicting entries",
                f"{_where(n)}: {n.get('Cache Evictions')} evictions, {n.get('Cache Overflows')} overflows, "
                f"hits {n.get('Cache Hits')} / misses {n.get('Cache Misses')}.",
                "Raise work_mem / hash_mem_multiplier or lower the number of distinct parameter values.", n.id))
    return out


# ------------------------------------------------------------------ driver

def run_rules(plan: Plan, cfg: Config) -> list[Finding]:
    findings: list[Finding] = []
    for r in RULES:
        findings.extend(r(plan, cfg))
    by_id = {n.id: n for n in plan.nodes}
    for f in findings:
        if f.node_id in by_id and not f.context:
            f.context = describe(by_id[f.node_id])
    findings.sort(key=lambda f: (-f.severity, -f.impact_ms, f.node_id or 0))
    return findings
