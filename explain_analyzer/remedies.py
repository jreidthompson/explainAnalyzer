"""Turn findings into an ordered, copy-pasteable action plan.

Every remedy carries
  * ``try_variants``  - statements that are safe to run inside BEGIN ... ROLLBACK to test the idea
  * ``apply``         - the statements to run for real (e.g. CREATE INDEX CONCURRENTLY)
  * ``caveats`` / ``verify`` - what can go wrong and what the next plan should look like

Stages (do them in this order, re-capturing the plan between stages, because each
fix can change the plan and invalidate the findings that come after it):
  1 refresh statistics / maintenance   cheap, safe, often enough
  2 structural                         indexes, extended statistics, query rewrites
  3 settings                           test per-transaction with SET LOCAL first
  4 investigate                        read-only queries that explain the finding
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field

from .condparse import Atom, index_plan, own_atoms, parse_atoms
from .context import join_conditions, tables
from .metrics import BLOCK_KB, Config
from .model import Finding, Node, Plan

STAGES = {
    1: "Refresh statistics and maintenance (cheap, safe - do first)",
    2: "Structural changes (indexes, extended statistics, query rewrites)",
    3: "Settings (prove with SET LOCAL in a transaction before changing anything permanent)",
    4: "Investigate (read-only queries that explain the finding)",
}
_RESERVED = {"user", "order", "group", "table", "select", "column", "check", "default", "desc", "asc", "end",
             "from", "grant", "limit", "offset", "primary", "references", "to", "union", "unique", "when",
             "where", "with", "all", "and", "any", "as", "both", "case", "cast", "create", "current_date",
             "distinct", "do", "else", "except", "false", "for", "foreign", "having", "in", "initially",
             "intersect", "into", "leading", "not", "null", "on", "only", "or", "placing", "returning",
             "some", "symmetric", "then", "trailing", "true", "using", "variadic", "window", "analyse",
             "analyze", "array"}


def qi(name: str) -> str:
    if re.fullmatch(r"[a-z_][a-z0-9_$]*", name) and name not in _RESERVED:
        return name
    return '"' + name.replace('"', '""') + '"'


def obj_name(prefix: str, table: str, cols: list[str]) -> str:
    base = "_".join([prefix, table] + [re.sub(r"\W+", "_", c).strip("_") for c in cols])
    base = re.sub(r"_+", "_", base).lower()
    if len(base) > 63:
        base = base[:54] + "_" + hashlib.sha256(base.encode()).hexdigest()[:8]
    return base


@dataclass
class Remedy:
    key: str
    kind: str                      # statistics | maintenance | index | rewrite | config | investigate
    stage: int
    title: str
    why: str
    try_variants: list[tuple[str, list[str]]] = field(default_factory=list)
    apply: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    verify: str = ""
    confidence: str = "medium"     # high | medium | low
    transactional: bool = True     # try statements can run inside BEGIN..ROLLBACK
    diagnostic_only: bool = False  # never deploy (e.g. enable_nestloop = off)
    manual: bool = False           # needs a human edit, nothing to run
    investigate_sql: list[str] = field(default_factory=list)
    nodes: list[int] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    impact_ms: float = 0.0
    id: str = ""
    _node_impact: dict = field(default_factory=dict, repr=False)

    @property
    def stage_name(self) -> str:
        return STAGES[self.stage]


class Ctx:
    def __init__(self, plan: Plan, cfg: Config, default_schema: str | None = None):
        self.plan, self.cfg = plan, cfg
        self.default_schema = default_schema
        self.remedies: dict[str, Remedy] = {}
        self.analyze: dict[str, set[int]] = {}          # table ref -> node ids needing fresh stats
        self.aliases: set[str] = set()
        self.by_alias: dict[str, tuple[str | None, str]] = {}
        for n in plan.nodes:
            rel = n.get("Relation Name")
            if rel:
                sch = self.sch(n)
                for k in {n.get("Alias") or rel, rel}:
                    self.aliases.add(k)
                    self.by_alias.setdefault(k, (sch, rel))

    # -- naming helpers
    def sch(self, node: Node) -> str | None:
        """Schema of a scanned relation: from the plan (VERBOSE / not on search_path) or --schema."""
        return node.get("Schema") or self.default_schema

    def table_ref(self, node: Node) -> tuple[str, str, str] | None:
        rel = node.get("Relation Name")
        if not rel:
            return None
        sch = self.sch(node)
        full = f"{qi(sch)}.{qi(rel)}" if sch else qi(rel)
        return full, rel, node.get("Alias") or rel

    def table_of_alias(self, alias: str | None, within: Node) -> tuple[str, str] | None:
        """(qualified name, bare name) for an alias used inside ``within``'s subtree."""
        for x in within.walk():
            rel = x.get("Relation Name")
            if rel and (alias is None or alias in (x.get("Alias"), rel)):
                sch = self.sch(x)
                return (f"{qi(sch)}.{qi(rel)}" if sch else qi(rel)), rel
        return None

    def add(self, r: Remedy, finding: Finding | None = None, node: Node | None = None) -> Remedy:
        cur = self.remedies.get(r.key)
        if cur is None:
            cur = self.remedies[r.key] = r
        if finding is not None:
            if finding.rule not in cur.rules:
                cur.rules.append(finding.rule)
            nid = finding.node_id if finding.node_id is not None else (node.id if node else None)
            if nid is not None:
                if nid not in cur.nodes:
                    cur.nodes.append(nid)
                cur._node_impact[nid] = max(cur._node_impact.get(nid, 0.0), finding.impact_ms)
            else:
                cur._node_impact[("plan", finding.rule)] = max(cur._node_impact.get(("plan", finding.rule), 0.0),
                                                              finding.impact_ms)
        return cur

    def need_analyze(self, node: Node) -> None:
        for x in node.walk():
            rel = x.get("Relation Name")
            if rel and not x.is_subplan_child():
                sch = self.sch(x)
                ref = f"{qi(sch)}.{qi(rel)}" if sch else qi(rel)
                self.analyze.setdefault(ref, set()).add(node.id)


# ---------------------------------------------------------------- generators

def _conds(n: Node, keys=("Index Cond", "Recheck Cond", "Filter")) -> list:
    out: list = []
    for k in keys:
        v = n.get(k)
        if v:
            out.extend(v if isinstance(v, list) else [v])
    return out


def _fmt_cols(cols: list[str]) -> str:
    return ", ".join(qi(c) for c in cols)


def _selective(n: Node, cfg: Config) -> bool:
    """True when the node discards most of what it reads, i.e. an index could actually help."""
    removed = float(n.get("Rows Removed by Filter") or 0) * n.m["loops"]
    kept = n.m.get("rows_total") or 0.0
    return removed + kept > 0 and removed / (removed + kept) >= cfg.filter_ratio


def _index_remedy(ctx: Ctx, f: Finding, n: Node) -> None:
    ref = ctx.table_ref(n)
    if not ref or n.node_type not in ("Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan"):
        return
    full, rel, alias = ref
    atoms = own_atoms(parse_atoms(_conds(n), ctx.aliases), {alias, rel})
    plan = index_plan(atoms)
    cond_txt = " AND ".join(_conds(n, ("Index Cond", "Recheck Cond", "Filter"))) or "(no condition)"
    ms = n.m["excl_ms"]
    verify = (f"{n.label()} (#{n.id}, {ms:,.0f} ms) should become an Index/Bitmap Scan on the new index and its "
              "time and 'Rows Removed by Filter' should fall sharply. Run ANALYZE on the table if the planner still "
              "prefers the sequential scan.")
    common_caveats = [
        "Writes to the table now also maintain this index; drop it later if pg_stat_user_indexes.idx_scan stays 0.",
        "CONCURRENTLY cannot run in a transaction and a failed run leaves an INVALID index to DROP; the plain "
        "CREATE INDEX in the experiment blocks writes while building - test on staging.",
    ]
    made = False
    if plan["columns"]:
        cols = plan["columns"]
        name = obj_name("idx", rel, cols)
        ddl = f"ON {full} ({_fmt_cols(cols)})"
        why = (f"{n.label()} evaluates `{cond_txt}` without a usable index"
               + (f", reading far more rows than it keeps ({n.m['loops']:,} loop(s))." if n.m["loops"] > 1 else "."))
        order = ("Column order: equality columns first, then at most one range column (PostgreSQL multicolumn-index rule)."
                 if len(cols) > 1 else "")
        cav = common_caveats + ([order] if order else [])
        if n.node_type in ("Index Scan", "Index Only Scan") and n.get("Index Name"):
            cav.append(f"Extends the access path of existing index {n.get('Index Name')}: keep the old index until "
                       "the new one is proven, then drop it if redundant.")
        cav += plan["notes"]
        ctx.add(Remedy(
            key=f"index:{full}:{','.join(cols)}", kind="index", stage=2,
            title=f"Create index on {full} ({_fmt_cols(cols)})", why=why,
            try_variants=[("plain index", [f"CREATE INDEX {name} {ddl};"])],
            apply=[f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} {ddl};"],
            caveats=cav, verify=verify, confidence="high" if ms >= 1 else "medium"), f, n)
        made = True
    for e in plan["expressions"]:
        ex = e if re.match(r"^\w+\(", e) else f"({e})"
        name = obj_name("idx", rel, ["expr", hashlib.sha256(e.encode()).hexdigest()[:6]])
        ctx.add(Remedy(
            key=f"index:{full}:expr:{e}", kind="index", stage=2,
            title=f"Create expression index on {full} ({e})",
            why=f"`{cond_txt}` applies an expression to a column, so a plain index cannot serve it.",
            try_variants=[("expression index", [f"CREATE INDEX {name} ON {full} ({ex});"])],
            apply=[f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {full} ({ex});"],
            caveats=common_caveats + ["The query must use exactly the same expression. Expression indexes cost more on "
                                      "INSERT/UPDATE (the expression is recomputed per row).",
                                      "Alternative: rewrite the predicate so it compares the bare column "
                                      "(e.g. a range instead of date_trunc()/cast on the column)."] + plan["notes"],
            verify=verify, confidence="high"), f, n)
        made = True
    for c in plan["trigram"]:
        name = obj_name("idx", rel, [c, "trgm"])
        ctx.add(Remedy(
            key=f"index:{full}:trgm:{c}", kind="index", stage=2,
            title=f"Create trigram GIN index on {full} ({qi(c)}) for LIKE '%...%'",
            why=f"`{cond_txt}` is an infix pattern match, which a btree cannot serve.",
            try_variants=[("trigram index", ["CREATE EXTENSION IF NOT EXISTS pg_trgm;",
                                             f"CREATE INDEX {name} ON {full} USING gin ({qi(c)} gin_trgm_ops);"])],
            apply=["CREATE EXTENSION IF NOT EXISTS pg_trgm;",
                   f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {full} USING gin ({qi(c)} gin_trgm_ops);"],
            caveats=common_caveats + ["pg_trgm is a trusted extension but still needs CREATE privilege on the database. "
                                      "Patterns shorter than 3 characters give the index nothing to look up. "
                                      "GIN indexes are slower to update than btrees."],
            verify=verify, confidence="medium"), f, n)
        made = True
    if not made and (plan["notes"] or atoms):
        ctx.add(Remedy(
            key=f"inv-index:{full}:{n.id}", kind="investigate", stage=4,
            title=f"Work out an access path for {full}",
            why=f"`{cond_txt}` could not be turned into an index automatically.",
            caveats=plan["notes"] or ["Conditions are not simple column comparisons."],
            investigate_sql=[f"SELECT indexrelid::regclass AS index, pg_get_indexdef(indexrelid) "
                             f"FROM pg_index WHERE indrelid = '{full}'::regclass;"],
            verify="Re-run EXPLAIN after rewriting the predicate or adding the index.", confidence="low"), f, n)


def _stats_remedy(ctx: Ctx, f: Finding, n: Node) -> None:
    """Row-estimate error that starts at node n."""
    if not n.m.get("mis_origin"):
        return
    ctx.need_analyze(n)
    dirn = "over" if n.m["mis_dir"] == "over" else "under"
    verify = (f"Node #{n.id} {n.label()} should estimate close to {n.m['act_rows']:,.0f} rows "
              f"(was {n.m['est_rows']:,.0f}); downstream joins/hashes should then change shape.")
    if n.node_type in ("Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan", "Bitmap Index Scan") and \
            n.get("Relation Name"):
        full, rel, alias = ctx.table_ref(n)
        atoms = own_atoms(parse_atoms(_conds(n), ctx.aliases), {alias, rel})
        cols: list[str] = []
        for a in atoms:
            if a.col and not a.func and a.col not in cols and a.kind in ("eq", "in", "range", "null", "join_eq", "join_range"):
                cols.append(a.col)
        exprs = [a.expr for a in atoms if a.func and a.expr and a.kind in ("eq", "in", "range")]
        if len(cols) >= 2:
            cols = cols[:4]
            name = obj_name("stx", rel, cols)
            ctx.add(Remedy(
                key=f"stats:{full}:{','.join(cols)}", kind="statistics", stage=2,
                title=f"Create extended statistics on {full} ({_fmt_cols(cols)})",
                why=(f"The planner {dirn}-estimated {n.label()} {n.m['mis']:,.0f}x. These columns are filtered together; "
                     "without multivariate statistics the planner multiplies their selectivities as if independent."),
                try_variants=[("extended statistics",
                               [f"CREATE STATISTICS {name} ON {_fmt_cols(cols)} FROM {full};", f"ANALYZE {full};"])],
                apply=[f"CREATE STATISTICS IF NOT EXISTS {name} ON {_fmt_cols(cols)} FROM {full};", f"ANALYZE {full};"],
                caveats=["ANALYZE after CREATE STATISTICS is required - the statistics are empty until then.",
                         "Functional-dependency statistics only help equality / IN conditions with constants; MCV lists "
                         "also cover ranges. Omitting the kind list builds every supported kind.",
                         "Extended statistics describe columns of ONE table; they do not fix join-selectivity errors.",
                         "Costs a little extra ANALYZE time and catalog space; create them only for column groups that "
                         "are actually queried together."],
                verify=verify, confidence="medium"), f, n)
        elif len(cols) == 1 and not exprs:
            c = cols[0]
            ctx.add(Remedy(
                key=f"stattarget:{full}:{c}", kind="statistics", stage=2,
                title=f"Raise the statistics target for {full}.{qi(c)}",
                why=(f"A single-column condition on {c} is {dirn}-estimated {n.m['mis']:,.0f}x; "
                     "a finer histogram / longer MCV list may fix skewed or irregular data."),
                try_variants=[("target 1000", [f"ALTER TABLE {full} ALTER COLUMN {qi(c)} SET STATISTICS 1000;",
                                               f"ANALYZE {full};"])],
                apply=[f"ALTER TABLE {full} ALTER COLUMN {qi(c)} SET STATISTICS 1000;", f"ANALYZE {full};"],
                caveats=["Default target is 100. Higher targets lengthen ANALYZE and planning slightly; "
                         "try 400-1000 and keep the smallest value that fixes the estimate.",
                         "If a plain ANALYZE already fixed it you do not need this."],
                verify=verify, confidence="low"), f, n)
        for e in exprs[:1]:
            name = obj_name("stx", rel, ["expr", hashlib.sha256(e.encode()).hexdigest()[:6]])
            ex = e if re.match(r"^\w+\(", e) else f"({e})"
            ctx.add(Remedy(
                key=f"stats:{full}:expr:{e}", kind="statistics", stage=2,
                title=f"Create expression statistics on {full} ({e}) (PostgreSQL 14+)",
                why="The condition applies an expression to a column; without expression statistics the planner "
                    "falls back to a fixed default selectivity.",
                try_variants=[("expression statistics",
                               [f"CREATE STATISTICS {name} ON ({ex}) FROM {full};", f"ANALYZE {full};"])],
                apply=[f"CREATE STATISTICS IF NOT EXISTS {name} ON ({ex}) FROM {full};", f"ANALYZE {full};"],
                caveats=["An expression index on the same expression also gathers these statistics."],
                verify=verify, confidence="medium"), f, n)
    elif n.node_type == "Aggregate" and n.get("Group Key"):
        gk = n.get("Group Key")
        gk = gk if isinstance(gk, list) else [gk]
        cols_by_tab: dict[str, list[str]] = {}
        for k in gk:
            m = re.fullmatch(r"(?:(\w+)\.)?(\w+)", k.strip())
            if not m:
                continue
            t = ctx.table_of_alias(m.group(1), n)
            if t:
                cols_by_tab.setdefault(t[0], []).append((m.group(2), t[1]))
        for full, items in cols_by_tab.items():
            cols = list(dict.fromkeys(c for c, _ in items))
            if len(cols) >= 2:
                rel = items[0][1]
                name = obj_name("stx_nd", rel, cols)
                ctx.add(Remedy(
                    key=f"stats-nd:{full}:{','.join(cols)}", kind="statistics", stage=2,
                    title=f"Create n-distinct statistics on {full} ({_fmt_cols(cols)})",
                    why=f"The number of groups produced by GROUP BY ({', '.join(gk)}) was {dirn}-estimated "
                        f"{n.m['mis']:,.0f}x.",
                    try_variants=[("ndistinct", [f"CREATE STATISTICS {name} (ndistinct) ON {_fmt_cols(cols)} FROM {full};",
                                                 f"ANALYZE {full};"])],
                    apply=[f"CREATE STATISTICS IF NOT EXISTS {name} (ndistinct) ON {_fmt_cols(cols)} FROM {full};",
                           f"ANALYZE {full};"],
                    caveats=["Only helps when the grouped columns come from one table (before joins)."],
                    verify=verify, confidence="medium"), f, n)
    elif n.node_type in ("Hash Join", "Merge Join", "Nested Loop"):
        conds = join_conditions(n)
        jcols: list[tuple[str, str, str]] = []
        for a in parse_atoms(conds, ctx.aliases):
            if a.kind == "join_eq" and a.col:
                t = ctx.table_of_alias(a.alias, n)
                if t:
                    jcols.append((t[0], t[1], a.col))
        ctx.add(Remedy(
            key=f"inv-join:{n.id}", kind="investigate", stage=4,
            title=f"Join estimate off {n.m['mis']:,.0f}x at #{n.id} {n.label()}",
            why=("Join-size errors usually come from stale statistics on the join columns, skewed key distributions, "
                 "or errors already present in the inputs. Extended statistics do not apply across tables."),
            caveats=["First run the ANALYZE step above, then re-capture the plan; if the error remains check "
                     "n_distinct for the join keys and for skew (most_common_freqs) with the query below."],
            investigate_sql=[
                f"SELECT tablename, attname, n_distinct, null_frac, "
                f"(most_common_freqs)[1] AS top_freq FROM pg_stats WHERE tablename IN "
                f"({', '.join(repr(t) for t in sorted({x[1] for x in jcols}) or ['<table>'])}) "
                f"AND attname IN ({', '.join(repr(c) for c in sorted({x[2] for x in jcols}) or ['<column>'])});"],
            verify=verify, confidence="low"), f, n)
        for full, rel, c in jcols[:2]:
            ctx.add(Remedy(
                key=f"stattarget:{full}:{c}", kind="statistics", stage=2,
                title=f"Raise the statistics target for join column {full}.{qi(c)}",
                why="Better n_distinct / MCV data for join keys improves join-size estimates.",
                try_variants=[("target 1000", [f"ALTER TABLE {full} ALTER COLUMN {qi(c)} SET STATISTICS 1000;",
                                               f"ANALYZE {full};"])],
                apply=[f"ALTER TABLE {full} ALTER COLUMN {qi(c)} SET STATISTICS 1000;", f"ANALYZE {full};"],
                caveats=["Low-confidence experiment; keep only if the join estimate actually improves."],
                verify=verify, confidence="low"), f, n)


def _mb(kb: float) -> int:
    return max(1, int(math.ceil(kb / 1024.0)))


def _work_mem_remedy(ctx: Ctx, f: Finding, n: Node) -> None:
    if f.rule == "sort-spill":
        disk = float(n.get("Sort Space Used") or 0)
        sizes = [_mb(disk * k) for k in (2, 4, 8)]
        ctx.add(Remedy(
            key=f"workmem:sort:{n.id}", kind="config", stage=3,
            title=f"Give the sort at #{n.id} enough work_mem to stay in memory",
            why=(f"{n.label()} spilled {disk / 1024:.1f} MB to disk. The in-memory form of a sort is larger than the "
                 "on-disk form, so test a few multiples of the spill size."),
            try_variants=[(f"work_mem {s}MB", [f"SET LOCAL work_mem = '{s}MB';"]) for s in dict.fromkeys(sizes)],
            apply=[f"-- per transaction (preferred): SET LOCAL work_mem = '{sizes[1]}MB';",
                   f"-- or for one role:            ALTER ROLE <role> SET work_mem = '{sizes[1]}MB';"],
            caveats=["work_mem is allowed PER sort/hash node, per backend, and per parallel worker - memory use "
                     "multiplies (PostgreSQL docs). Do not raise it globally for one query.",
                     "Use the smallest value whose plan shows 'Sort Method: quicksort'."],
            verify=f"Sort #{n.id} should report 'Sort Method: quicksort  Memory: ...' with no 'Disk:'.",
            confidence="high", transactional=True), f, n)
        _sort_index(ctx, f, n)
    elif f.rule == "hash-spill" and n.get("Hash Batches"):
        b = n.get("Hash Batches") or 1
        orig = n.get("Original Hash Batches") or b
        if orig < b:
            peak = float(n.get("Peak Memory Usage") or 0)
            need = peak * b * 1.25
            wm = ctx.plan.m.get("work_mem_kb")
            mult = max(2.0, math.ceil(need / wm)) if wm else 4.0
            ctx.add(Remedy(
                key=f"workmem:hash:{n.id}", kind="config", stage=3,
                title=f"Let the hash at #{n.id} stay in memory (batches grew {orig}->{b} at runtime)",
                why="The hash table outgrew its memory limit while being built, so it was split into batches on disk.",
                try_variants=[(f"hash_mem_multiplier {mult:g}", [f"SET LOCAL hash_mem_multiplier = {mult:g};"]),
                              (f"work_mem {_mb(need)}MB", [f"SET LOCAL work_mem = '{_mb(need)}MB';"])],
                apply=[f"-- per transaction: SET LOCAL hash_mem_multiplier = {mult:g};"],
                caveats=["hash_mem_multiplier raises the limit for hash nodes only (sorts keep work_mem), "
                         "so it is the narrower tool. Memory multiplies per hash node, backend and worker."],
                verify=f"Hash #{n.id} should show 'Batches: 1'.", confidence="medium"), f, n)
    elif f.rule == "agg-spill":
        peak, disk = float(n.get("Peak Memory Usage") or 0), float(n.get("Disk Usage") or 0)
        need = (peak + disk) * 1.5
        ctx.add(Remedy(
            key=f"workmem:agg:{n.id}", kind="config", stage=3,
            title=f"Let the hash aggregate at #{n.id} stay in memory",
            why=f"The aggregate spilled {disk / 1024:.1f} MB to disk.",
            try_variants=[(f"work_mem {_mb(need)}MB", [f"SET LOCAL work_mem = '{_mb(need)}MB';"]),
                          (f"hash_mem_multiplier 4", ["SET LOCAL hash_mem_multiplier = 4;"])],
            apply=[f"-- per transaction: SET LOCAL work_mem = '{_mb(need)}MB';"],
            caveats=["Check the group-count estimate first: a wildly over-estimated group count can force spilling "
                     "that more memory only hides."],
            verify=f"Aggregate #{n.id} should show 'Batches: 1' and no 'Disk Usage'.", confidence="medium"), f, n)


def _sort_index(ctx: Ctx, f: Finding, n: Node) -> None:
    keys = n.get("Sort Key")
    keys = keys if isinstance(keys, list) else ([keys] if keys else [])
    if not keys:
        return
    parsed, table = [], None
    for k in keys:
        m = re.fullmatch(r"(?:(\w+)\.)?(\w+)(\s+DESC)?(\s+NULLS (?:FIRST|LAST))?", k.strip())
        if not m:
            return
        t = ctx.table_of_alias(m.group(1), n)
        if not t or (table and t != table):
            return
        table = t
        parsed.append(qi(m.group(2)) + (m.group(3) or "") + (m.group(4) or ""))
    full, rel = table
    cols = [p.split()[0].strip('"') for p in parsed]
    name = obj_name("idx_ord", rel, cols)
    top_n = n.parent is not None and n.parent.node_type == "Limit"
    ctx.add(Remedy(
        key=f"index:{full}:order:{','.join(parsed)}", kind="index", stage=2,
        title=f"Create index on {full} ({', '.join(parsed)}) to provide the sort order",
        why=("All sort keys belong to one table, so an index in that order can replace the sort"
             + (" (the sort feeds a LIMIT, where an ordered index scan can stop early)." if top_n else ".")),
        try_variants=[("ordering index", [f"CREATE INDEX {name} ON {full} ({', '.join(parsed)});"])],
        apply=[f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {full} ({', '.join(parsed)});"],
        caveats=["Only helps if the query can read this table in index order (it is the driving table, or a LIMIT "
                 "lets the planner stop early); otherwise the planner will keep sorting.",
                 "Direction and NULLS FIRST/LAST must match the ORDER BY."],
        verify=f"The Sort at #{n.id} should disappear or become an Incremental Sort.", confidence="low"), f, n)


def _maintenance(ctx: Ctx, f: Finding, n: Node) -> None:
    ref = ctx.table_ref(n)
    if not ref:
        return
    full = ref[0]
    ctx.add(Remedy(
        key=f"vacuum:{full}", kind="maintenance", stage=1,
        title=f"VACUUM {full} so index-only scans stop visiting the heap",
        why=(f"{n.label()} did {n.get('Heap Fetches'):,} heap fetches: the visibility map is stale, so the 'index-only' "
             "scan still reads table pages."),
        try_variants=[("vacuum (outside transaction)", [f"VACUUM (ANALYZE) {full};"])],
        apply=[f"VACUUM (ANALYZE) {full};",
               f"-- keep it fresh: ALTER TABLE {full} SET (autovacuum_vacuum_scale_factor = 0.02, "
               f"autovacuum_vacuum_insert_scale_factor = 0.05);"],
        caveats=["VACUUM cannot run inside a transaction block, so it is not rolled back (it is a safe maintenance "
                 "operation). Long-running transactions elsewhere can prevent it from marking pages all-visible.",
                 "Per-table autovacuum settings override the global ones for that table only."],
        verify=f"'Heap Fetches' on #{n.id} should drop towards 0.", confidence="high", transactional=False), f, n)


def _subplan_rewrite(ctx: Ctx, f: Finding, n: Node) -> None:
    sp = next((a for a in [n, *n.ancestors()] if a.relationship == "SubPlan"), None)
    if sp is None or n.m["loops"] < 100:
        return
    scan = next((x for x in sp.walk() if x.get("Relation Name")), None)
    if scan is None:
        return
    full, rel, alias = ctx.table_ref(scan)
    atoms = parse_atoms(_conds(scan), ctx.aliases)
    corr = [a for a in atoms if a.kind == "join_eq" and a.col]
    colname = corr[0].col if corr else "<key>"
    outer = sorted(corr[0].refs)[0] if corr and corr[0].refs else "<outer>"
    template = (f"-- Replace the correlated sub-select (run {n.m['loops']:,} times) with ONE aggregation joined in:\n"
                f"--   SELECT ..., agg.value\n--   FROM <outer table> {outer}\n"
                f"--   LEFT JOIN (SELECT {qi(colname)}, <aggregate>(...) AS value\n"
                f"--              FROM {full} GROUP BY {qi(colname)}) agg ON agg.{qi(colname)} = {outer}.<key>\n"
                f"-- or, when you need top-N per row, use LEFT JOIN LATERAL (SELECT ... FROM {full} WHERE "
                f"{qi(colname)} = {outer}.<key> ORDER BY ... LIMIT n) x ON true")
    ctx.add(Remedy(
        key=f"rewrite:subplan:{sp.id}", kind="rewrite", stage=2,
        title=f"Rewrite the correlated sub-select over {full} (SubPlan #{sp.id})",
        why=(f"A SubPlan executes once per outer row: {n.m['loops']:,} times here, "
             f"{(sp.m['incl_ms'] or 0):,.0f} ms in total."),
        manual=True, investigate_sql=[template],
        caveats=["A join with GROUP BY computes every group once - best when most outer rows are needed; for a few "
                 "outer rows an index on the correlated column (see the index action) is the smaller change.",
                 "LEFT JOIN keeps outer rows with no match, like the scalar sub-select did (it returned NULL)."],
        verify="The SubPlan node should disappear and the scan below it should run once (loops=1).",
        confidence="medium"), f, n)


def _jit(ctx: Ctx, f: Finding) -> None:
    ctx.add(Remedy(
        key="config:jit", kind="config", stage=3,
        title="Turn JIT off (or raise its cost thresholds) for this kind of query",
        why=f.title,
        try_variants=[("jit = off", ["SET LOCAL jit = off;"])],
        apply=["-- this query/transaction: SET LOCAL jit = off;",
               "-- OLTP-style workloads: ALTER DATABASE <db> SET jit = off;",
               "-- or keep JIT for big queries: raise jit_above_cost / jit_inline_above_cost / jit_optimize_above_cost"],
        caveats=["JIT pays off for long, CPU-bound analytic queries; for short queries compile time dominates. "
                 "A JIT-heavy plan often means the planner over-estimated cost - check estimates too."],
        verify="The JIT: section (and its Timing) should vanish from the plan.", confidence="high"), f)


def _nested_loop(ctx: Ctx, f: Finding, n: Node) -> None:
    ctx.add(Remedy(
        key=f"diag:nestloop:{n.id}", kind="config", stage=3,
        title=f"Diagnostic: see what the planner would do without nested loops (#{n.id})",
        why="If the plan gets much faster, the nested loop was a bad choice caused by an estimate or a missing index.",
        try_variants=[("enable_nestloop = off", ["SET LOCAL enable_nestloop = off;"])],
        apply=[], diagnostic_only=True,
        caveats=["DIAGNOSTIC ONLY - do not deploy. Planner enable_* switches only discourage a method and hide the "
                 "real cause. Fix the cause: the outer row estimate (statistics) or an index on the inner join column."],
        verify="Compare execution time with the baseline; if faster, work on the estimates / indexes above instead.",
        confidence="medium"), f, n)
    # the inner side's access path is handled by the seq-scan / rows-removed rules


def _investigate(ctx: Ctx, f: Finding, n: Node | None) -> None:
    r = f.rule
    if r == "disk-reads" and n is not None:
        names = sorted({t.split()[0] for t in tables(n)})[:6]
        lst = ", ".join(f"'{x}'" for x in names)
        ctx.add(Remedy(
            key=f"inv:reads:{n.id}", kind="investigate", stage=4,
            title=f"Check for bloat / cold cache under #{n.id}",
            why=f.detail,
            investigate_sql=[
                "SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) AS total, n_live_tup, n_dead_tup, "
                f"last_autovacuum, last_autoanalyze FROM pg_stat_user_tables WHERE relname IN ({lst});",
                "-- run the same EXPLAIN a second time: if reads turn into hits it was a cold cache."],
            caveats=["Dead tuples >> live tuples or a table far larger than its row count suggests means bloat: "
                     "VACUUM, REINDEX ... CONCURRENTLY (PG12+), or pg_repack/pg_squeeze for tables.",
                     "Blocks per returned row is the number to watch: lots of blocks for few rows means a "
                     "non-selective index or poor locality (a covering index can help)."],
            verify="Fewer shared read/hit blocks for the same rows.", confidence="low"), f, n)
    elif r == "workers-launched" and n is not None:
        ctx.add(Remedy(
            key="inv:workers", kind="investigate", stage=4,
            title="Why parallel workers were not all launched", why=f.title,
            investigate_sql=["SHOW max_worker_processes; SHOW max_parallel_workers; SHOW max_parallel_workers_per_gather;",
                             "SELECT count(*) AS busy_parallel_workers FROM pg_stat_activity "
                             "WHERE backend_type = 'parallel worker';"],
            caveats=["Workers come from a shared pool; under concurrent load the query silently runs with fewer."],
            verify="Workers Launched equals Workers Planned.", confidence="low"), f, n)
    elif r == "planning-time":
        ctx.add(Remedy(
            key="inv:planning", kind="investigate", stage=4,
            title="Reduce planning time", why=f.title,
            investigate_sql=["SHOW plan_cache_mode; SHOW join_collapse_limit; SHOW from_collapse_limit;"],
            caveats=["Use prepared statements (the first 5 executions plan per call, then a generic plan can be reused).",
                     "Many partitions/joins inflate planning: prune partitions with constant predicates, or reduce "
                     "join_collapse_limit for very wide joins."],
            verify="Planning Time falls on repeated executions of the prepared statement.", confidence="low"), f, None)
    elif r == "trigger":
        m = re.search(r"Trigger (\S+)", f.title)
        nm = m.group(1) if m else "<trigger>"
        ctx.add(Remedy(
            key=f"inv:trigger:{nm}", kind="investigate", stage=4,
            title=f"Look at trigger {nm}", why=f.detail,
            investigate_sql=[f"SELECT conrelid::regclass AS table, conname, pg_get_constraintdef(oid) "
                             f"FROM pg_constraint WHERE conname = '{nm.split('_')[0] if nm.startswith('RI_') else nm}';",
                             "-- FK triggers (RI_ConstraintTrigger_*) are slow when the referencing column has no index: "
                             "create an index on the foreign-key columns of the referencing table."],
            caveats=["For RI_ConstraintTrigger_* look up the constraint name shown in the plan's trigger line."],
            verify="Trigger time falls.", confidence="low"), f, None)
    elif r == "many-partitions" and n is not None:
        ctx.add(Remedy(
            key=f"inv:partitions:{n.id}", kind="investigate", stage=4,
            title=f"Prune partitions under #{n.id}", why=f.detail,
            investigate_sql=["SHOW enable_partition_pruning;"],
            caveats=["Pruning needs conditions on the partition key compared with constants/parameters; "
                     "non-immutable functions such as now() are only pruned at execution time, and expressions "
                     "wrapped around the key prevent it."],
            verify="Fewer child scans (or 'Subplans Removed') under the Append.", confidence="low"), f, n)


# ----------------------------------------------------------------- driver

def build_actions(plan: Plan, findings: list[Finding], cfg: Config,
                  default_schema: str | None = None) -> list[Remedy]:
    ctx = Ctx(plan, cfg, default_schema)
    by_id = {n.id: n for n in plan.nodes}
    for f in findings:
        n = by_id.get(f.node_id) if f.node_id else None
        r = f.rule
        try:
            if r in ("seq-scan-filter", "seq-scan-loops", "rows-removed") and n is not None:
                if r == "rows-removed" and "Index Recheck" in f.title:
                    ctx.need_analyze(n)
                _index_remedy(ctx, f, n)
                _subplan_rewrite(ctx, f, n)
            elif r == "hot-node" and n is not None:
                if n.node_type == "Seq Scan" and n.get("Filter") and _selective(n, cfg):
                    _index_remedy(ctx, f, n)
                _subplan_rewrite(ctx, f, n)
            elif r == "row-misestimate" and n is not None:
                _stats_remedy(ctx, f, n)
            elif r in ("sort-spill", "agg-spill", "hash-spill") and n is not None:
                _work_mem_remedy(ctx, f, n)
                if r == "hash-spill":
                    _hash_origin(ctx, f, n, cfg)
            elif r == "heap-fetches" and n is not None:
                _maintenance(ctx, f, n)
            elif r == "jit":
                _jit(ctx, f)
            elif r == "nested-loop" and n is not None:
                _nested_loop(ctx, f, n)
            else:
                _investigate(ctx, f, n)
        except Exception as e:  # a remedy bug must never break the report
            ctx.remedies.setdefault(f"error:{r}:{f.node_id}", Remedy(
                key=f"error:{r}:{f.node_id}", kind="investigate", stage=4, title=f"(no automatic action for {r})",
                why=f"internal: {type(e).__name__}: {e}", confidence="low"))

    # one consolidated ANALYZE step
    if ctx.analyze:
        tabs = sorted(ctx.analyze)
        nodes = sorted({i for s in ctx.analyze.values() for i in s})
        ctx.add(Remedy(
            key="analyze:" + ",".join(tabs), kind="maintenance", stage=1,
            title="ANALYZE the tables behind the row-estimate errors",
            why=("Cheapest possible fix: stale or missing statistics are the most common cause of a bad estimate, "
                 "and every later finding may disappear once estimates are right."),
            try_variants=[("analyze", [f"ANALYZE {', '.join(tabs)};"])],
            apply=[f"ANALYZE {', '.join(tabs)};"],
            caveats=["Autovacuum does not analyze partitioned parent tables or foreign tables - analyze those by hand.",
                     "ANALYZE samples rows, so estimates can wobble slightly between runs.",
                     "If estimates are still wrong afterwards, move on to the extended-statistics actions."],
            verify="Re-capture the plan: the 'Row estimate off by' findings should shrink or vanish.",
            confidence="medium", nodes=nodes, rules=["row-misestimate"]))
        rem = ctx.remedies["analyze:" + ",".join(tabs)]
        for nid in nodes:
            rem._node_impact[nid] = by_id[nid].m.get("excl_ms", 0.0) if nid in by_id else 0.0

    out = list(ctx.remedies.values())
    for r in out:
        r.impact_ms = sum(r._node_impact.values())
    out.sort(key=lambda r: (r.stage, -r.impact_ms, r.title))
    for i, r in enumerate(out, 1):
        r.id = f"A{i}"
    ids = {}
    for r in out:
        for nid in r.nodes:
            ids.setdefault(nid, []).append(r.id)
    for f in findings:
        f.actions = [a for a in ids.get(f.node_id, []) if f.node_id is not None]
    return out


def _hash_origin(ctx: Ctx, f: Finding, n: Node, cfg: Config) -> None:
    """A planned-up-front hash split is an estimate problem: attach the origin's stats remedies."""
    orig = n.get("Original Hash Batches") or n.get("Hash Batches") or 1
    if orig < (n.get("Hash Batches") or 1):
        return
    from .rules import origin_nodes
    srcs = origin_nodes(n, cfg) if n.m.get("mis_dir") == "over" else []
    for o in srcs:
        _stats_remedy(ctx, f, o)
    if n.m.get("mis_origin"):
        child = next((c for c in n.children if not c.is_subplan_child()), None)
        if child is not None:
            ctx.need_analyze(child)
