"""Derived per-node metrics (exclusive time, row-estimate error, buffers...)."""
from __future__ import annotations

import re
from dataclasses import dataclass, fields

from .model import Node, Plan

BLOCK_KB = 8
BUF_KINDS = ("Shared", "Local", "Temp")


@dataclass
class Config:
    """Rule thresholds. Override from the CLI with ``--set name=value``."""
    hot_min_ms: float = 1.0
    hot_info_pct: float = 10.0
    hot_warn_pct: float = 25.0
    hot_crit_pct: float = 50.0
    mis_warn: float = 10.0
    mis_crit: float = 100.0
    mis_min_rows: int = 100          # min |actual-estimate| in total rows
    filter_min_removed: int = 10000  # rows removed before a filter rule fires
    filter_ratio: float = 0.9        # removed / (removed + kept)
    nl_loops: int = 1000
    nl_inner_pct: float = 10.0
    heap_fetch_min: int = 1000
    heap_fetch_ratio: float = 0.2
    read_min_blocks: int = 1280      # 10 MB of shared reads
    read_ratio: float = 0.5
    jit_warn_pct: float = 10.0
    jit_crit_pct: float = 30.0
    planning_warn_pct: float = 50.0
    planning_min_ms: float = 10.0
    trigger_pct: float = 10.0
    many_partitions: int = 50
    cost_hot_pct: float = 50.0       # used when the plan has no ANALYZE data

    @classmethod
    def from_overrides(cls, pairs: list[str]) -> "Config":
        cfg = cls()
        types = {f.name: f.type for f in fields(cls)}
        for pair in pairs:
            if "=" not in pair:
                raise ValueError(f"expected name=value, got {pair!r}")
            k, v = pair.split("=", 1)
            if k not in types:
                raise ValueError(f"unknown threshold {k!r}; valid: {', '.join(types)}")
            setattr(cfg, k, int(v) if types[k] in (int, "int") else float(v))
        return cfg


def _f(v, default=0.0) -> float:
    return default if v is None else float(v)


def _is_init(n: Node) -> bool:
    return n.relationship == "InitPlan"


def compute(plan: Plan, cfg: Config) -> None:
    """Fill ``node.m`` for every node and ``plan.m`` with plan-level totals."""
    analyzed = plan.has_analyze
    plan.m["analyzed"] = analyzed
    _participants(plan.root, 1)

    for n in plan.nodes:
        m = n.m
        loops = n.get("Actual Loops")
        m["never"] = bool(n.get("Never Executed")) or loops == 0
        m["loops"] = loops if loops is not None else 1
        m["est_rows"] = _f(n.get("Plan Rows"))
        if analyzed:
            m["act_rows"] = _f(n.get("Actual Rows"))
            m["rows_total"] = m["act_rows"] * m["loops"]
            t = n.get("Actual Total Time")
            m["incl_ms"] = (_f(t) * m["loops"] / m["par"]) if t is not None else None
        else:
            m["act_rows"] = m["rows_total"] = None
            m["incl_ms"] = None

    if analyzed:
        _exclusive_time(plan)
    else:
        for n in plan.nodes:
            n.m["excl_ms"] = 0.0
    _cost_metrics(plan)
    _row_errors(plan, cfg)
    _buffers(plan)

    denom = plan.execution_time or plan.root.m.get("incl_ms") or 0.0
    plan.m["total_ms"] = denom
    for n in plan.nodes:
        n.m["excl_pct"] = (n.m["excl_ms"] / denom * 100.0) if analyzed and denom else 0.0
        n.m["incl_pct"] = (n.m["incl_ms"] / denom * 100.0) if analyzed and denom and n.m["incl_ms"] is not None else 0.0
    plan.m["buf_total"] = {k: plan.root.m["buf_incl"].get(k, 0) for k in plan.root.m["buf_incl"]}


def _participants(n: Node, par: int) -> None:
    n.m["par"] = par
    child_par = par
    if n.node_type in ("Gather", "Gather Merge"):
        launched = n.get("Workers Launched")
        child_par = (int(launched) + 1) if launched is not None else par
    for c in n.children:
        _participants(c, child_par)


def _exclusive_time(plan: Plan) -> None:
    for n in plan.nodes:
        incl = n.m["incl_ms"] or 0.0
        sub = sum((c.m["incl_ms"] or 0.0) for c in n.children if not _is_init(c))
        n.m["excl_ms"] = max(incl - sub, 0.0)
    # InitPlans / CTEs run lazily inside whichever node first uses them; the
    # parent's inclusive time does not include them separately, so deduct them
    # from the consumer instead of from the parent.
    for n in plan.nodes:
        if not _is_init(n):
            continue
        consumer = _find_consumer(plan, n)
        if consumer is not None:
            consumer.m["excl_ms"] = max(consumer.m["excl_ms"] - (n.m["incl_ms"] or 0.0), 0.0)
            n.m["consumed_by"] = consumer.id


def _subtree(n: Node) -> set[int]:
    return {x.id for x in n.walk()}


def _find_consumer(plan: Plan, init: Node) -> Node | None:
    name = init.subplan_name
    inside = _subtree(init)
    if name.startswith("CTE "):
        cte = name[4:].strip()
        for x in plan.nodes:
            if x.id not in inside and x.node_type == "CTE Scan" and x.get("CTE Name") == cte:
                return x
        return None
    m = re.match(r"InitPlan (\d+)(?: \(returns ([^)]*)\))?", name)
    if not m:
        return None
    pats = [re.compile(rf"\(InitPlan {m.group(1)}\)")]
    if m.group(2):
        for tok in re.findall(r"\$\d+", m.group(2)):
            pats.append(re.compile(re.escape(tok) + r"(?!\d)"))
    for x in plan.nodes:
        if x.id in inside:
            continue
        for k, v in x.props.items():
            vals = v if isinstance(v, list) else [v]
            if any(isinstance(s, str) and any(p.search(s) for p in pats) for s in vals):
                return x
    return None


def _cost_metrics(plan: Plan) -> None:
    root_cost = _f(plan.root.get("Total Cost"))
    for n in plan.nodes:
        tc = _f(n.get("Total Cost"))
        sub = sum(_f(c.get("Total Cost")) for c in n.children if not c.is_subplan_child())
        # Limit & friends stop early so child cost can exceed parent cost.
        n.m["excl_cost"] = max(tc - sub, 0.0)
        n.m["cost_pct"] = (n.m["excl_cost"] / root_cost * 100.0) if root_cost else 0.0


def _row_errors(plan: Plan, cfg: Config) -> None:
    if not plan.m["analyzed"]:
        for n in plan.nodes:
            n.m.update(mis=1.0, mis_dir=None, mis_origin=False, mis_ignored=None)
        return
    for n in plan.nodes:
        m = n.m
        est, act = m["est_rows"], m["act_rows"]
        m["mis"], m["mis_dir"], m["mis_ignored"] = 1.0, None, None
        if m["never"]:
            m["mis_ignored"] = "never executed"
            continue
        e, a = max(est, 1.0), max(act, 1.0)
        factor = max(e, a) / min(e, a)
        direction = "under" if a > e else "over"
        if factor < 1.0001:
            continue
        m["mis"], m["mis_dir"] = factor, direction
        if abs(act - est) * m["loops"] < cfg.mis_min_rows and factor < 1000:
            m["mis_ignored"] = "small absolute difference"
        elif direction == "over" and any(a_.node_type in ("Limit",) for a_ in n.ancestors()):
            m["mis_ignored"] = "over-estimate below a Limit (early termination)"
        elif direction == "over" and n.parent is not None and n.parent.node_type == "Merge Join":
            m["mis_ignored"] = "over-estimate below a Merge Join (early termination)"
        elif direction == "over" and n.parent is not None and \
                (n.parent.get("Join Type") in ("Semi", "Anti")) and n.relationship == "Inner":
            m["mis_ignored"] = "inner side of semi/anti join"
    thr = cfg.mis_warn
    for n in plan.nodes:
        m = n.m
        bad = m["mis"] >= thr and not m["mis_ignored"]
        m["mis_origin"] = False
        if bad:
            kids = [c for c in n.children if not c.is_subplan_child()]
            propagated = any(c.m["mis"] >= thr and not c.m["mis_ignored"]
                             and c.m["mis_dir"] == m["mis_dir"] for c in kids)
            m["mis_origin"] = not propagated


def _buf(n: Node) -> dict[str, int]:
    out = {}
    for kind in BUF_KINDS:
        for what in ("Hit", "Read", "Dirtied", "Written"):
            k = f"{kind} {what} Blocks"
            if k in n.props:
                out[k] = int(n.props[k] or 0)
    # temp blocks only have Read/Written variants but share the naming scheme
    return out


def _buffers(plan: Plan) -> None:
    plan.m["has_buffers"] = any(_buf(n) for n in plan.nodes)
    for n in plan.nodes:
        n.m["buf_incl"] = _buf(n)
    for n in plan.nodes:
        incl = n.m["buf_incl"]
        ex = dict(incl)
        for c in n.children:
            if _is_init(c):
                continue
            for k, v in c.m["buf_incl"].items():
                if k in ex:
                    ex[k] = max(ex[k] - v, 0)
        n.m["buf_excl"] = ex
        hit, read = ex.get("Shared Hit Blocks", 0), ex.get("Shared Read Blocks", 0)
        n.m["hit_ratio"] = hit / (hit + read) if (hit + read) else None
        n.m["io_read_ms"] = _f(n.get("I/O Read Time")) + _f(n.get("Shared I/O Read Time")) + \
            _f(n.get("Local I/O Read Time"))
