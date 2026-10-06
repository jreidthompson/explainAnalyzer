"""Describe *where* in a query a plan node sits: path from the root, the tables
beneath it, and for joins the two sides plus the join condition."""
from __future__ import annotations

import re

from .model import Node

JOINS = ("Nested Loop", "Hash Join", "Merge Join")
COND_KEYS = ("Hash Cond", "Merge Cond", "Join Filter", "Index Cond", "Recheck Cond", "Filter",
             "Cache Key")
_SCANS = ("Scan",)


def _real_children(n: Node) -> list[Node]:
    return [c for c in n.children if not c.is_subplan_child()]


def table_name(n: Node) -> str | None:
    """Short name for a node that reads a relation, else None."""
    p = n.props
    rel = p.get("Relation Name")
    if rel:
        sch = p.get("Schema")
        full = f"{sch}.{rel}" if sch else rel
        alias = p.get("Alias")
        return f"{full} {alias}" if alias and alias != rel else full
    if n.node_type == "CTE Scan" and p.get("CTE Name"):
        return f"CTE {p['CTE Name']}" + (f" {p['Alias']}" if p.get("Alias") and p["Alias"] != p["CTE Name"] else "")
    if n.node_type == "Function Scan" and p.get("Function Name"):
        return f"{p['Function Name']}()" + (f" {p['Alias']}" if p.get("Alias") else "")
    if n.node_type in ("Subquery Scan", "Values Scan") and p.get("Alias"):
        return p["Alias"]
    return None


def tables(n: Node) -> list[str]:
    """Distinct tables read in the subtree, in plan order (subplans excluded)."""
    seen: list[str] = []

    def rec(x: Node) -> None:
        t = table_name(x)
        if t and t not in seen:
            seen.append(t)
        for c in _real_children(x):
            rec(c)
    rec(n)
    return seen


def _aliases(n: Node) -> set[str]:
    out = set()
    for x in n.walk():
        for k in ("Alias", "Relation Name", "CTE Name"):
            if x.props.get(k):
                out.add(x.props[k])
    return out


def side(n: Node) -> tuple[Node | None, Node | None]:
    ch = _real_children(n)
    if n.node_type in JOINS and len(ch) >= 2:
        return ch[0], ch[1]
    return None, None


def chain(n: Node) -> str:
    """Describe a side as e.g. 'Memoize > Index Scan using i on t x' (through single-child wrappers)."""
    parts = []
    x = n
    while True:
        parts.append(x.label())
        ch = _real_children(x)
        if len(ch) == 1 and x.node_type in ("Memoize", "Materialize", "Hash", "Sort", "Incremental Sort",
                                              "Gather", "Gather Merge", "Result", "Unique", "Limit"):
            x = ch[0]
            continue
        break
    return " > ".join(parts)


_BARE = re.compile(r"(?<=\()(?P<col>[A-Za-z_]\w*)(?=\s*(?:=|<>|<=|>=|<|>|~~\*?|!~~\*?|@>|<@|&&)\s)")


def qualify(node: Node, cond):
    """Without VERBOSE, columns of the scanned table are unqualified in its own
    Index Cond/Recheck Cond/Filter ('(id = t.k)'). Prefix them with the node's alias."""
    alias = node.props.get("Alias") or node.props.get("Relation Name")
    if not alias:
        return cond
    f = lambda s: _BARE.sub(lambda m: f"{alias}.{m.group('col')}", s) if isinstance(s, str) else s
    return [f(s) for s in cond] if isinstance(cond, list) else f(cond)


def join_conditions(n: Node) -> list[str]:
    """Join condition(s): the join node's own conds, plus conditions in the inner
    subtree that reference tables from the outer side (parameterised nested loops)."""
    conds: list[str] = []

    def add(v) -> None:
        for s in (v if isinstance(v, list) else [v]):
            if isinstance(s, str) and s not in conds:
                conds.append(s if len(s) <= 240 else s[:237] + "...")

    for k in ("Hash Cond", "Merge Cond", "Join Filter"):
        if n.props.get(k):
            add(n.props[k])
    outer, inner = side(n)
    if outer is not None and inner is not None:
        names = {a for a in _aliases(outer) if a}
        if names:
            pat = re.compile(r"(?<![\w.])(?:" + "|".join(re.escape(a) for a in sorted(names, key=len, reverse=True)) + r")\.")
            for x in inner.walk():
                for k in ("Index Cond", "Recheck Cond", "Filter", "Cache Key"):
                    v = x.props.get(k)
                    vals = v if isinstance(v, list) else [v]
                    if any(isinstance(s, str) and pat.search(s) for s in vals):
                        add(qualify(x, v))
    return conds


def path(n: Node, keep: int = 5) -> str:
    chain_ = list(reversed(list(n.ancestors()))) + [n]
    shown = chain_[-keep:]
    parts = [f"#{x.id} {x.node_type if x is not n else x.label()}" for x in shown]
    if len(chain_) > keep:
        parts.insert(0, "...")
    return " > ".join(parts)


def describe(n: Node, limit: int = 8) -> list[str]:
    """Human-readable context lines for a node."""
    lines = [f"Location: {path(n)}"]
    outer, inner = side(n)
    if outer is not None:
        jt = n.props.get("Join Type") or "Inner"
        lines.append(f"Join ({jt}): outer #{outer.id} [{', '.join(tables(outer)[:limit]) or '?'}]"
                     f"  with  inner #{inner.id} [{', '.join(tables(inner)[:limit]) or '?'}]")
        lines.append(f"Inner side: {chain(inner)}")
        for c in join_conditions(n):
            lines.append(f"On: {c}")
    else:
        t = tables(n)
        if t:
            more = f" (+{len(t) - limit} more)" if len(t) > limit else ""
            lines.append(("Table: " if len(t) == 1 and table_name(n) else "Tables: ") + ", ".join(t[:limit]) + more)
        if n.node_type == "Memoize" and n.props.get("Cache Key"):
            lines.append(f"Cache key: {n.props['Cache Key']}")
    return lines


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 3] + "..."


def join_on(n: Node, maxlen: int = 140) -> str:
    """Join condition for a join node (table.column = table.column), or the index
    lookup condition for an index scan. Empty string for other nodes."""
    if n.node_type in JOINS:
        conds = join_conditions(n)
        return _clip(" AND ".join(conds), maxlen) if conds else ""
    if n.node_type in ("Index Scan", "Index Only Scan", "Bitmap Index Scan"):
        v = qualify(n, n.props.get("Index Cond"))
        if v:
            return _clip(" AND ".join(v) if isinstance(v, list) else v, maxlen)
    return ""


def join_sides(n: Node, maxlen: int = 110) -> str:
    """'outer tables <-> inner tables' for a join node, else ''."""
    outer, inner = side(n)
    if outer is None:
        return ""
    a, b = ", ".join(tables(outer)[:3]) or "?", ", ".join(tables(inner)[:3]) or "?"
    ta, tb = len(tables(outer)), len(tables(inner))
    a += f" (+{ta - 3})" if ta > 3 else ""
    b += f" (+{tb - 3})" if tb > 3 else ""
    return _clip(f"{a}  <->  {b}", maxlen)


def _join_val(v) -> str:
    return ", ".join(str(x) for x in v) if isinstance(v, list) else str(v)


def _kb(kb) -> str:
    kb = float(kb)
    return f"{kb / 1024 ** 2:.1f}GB" if kb >= 1024 ** 2 else f"{kb / 1024:.1f}MB" if kb >= 1024 else f"{kb:.0f}kB"


def keys(n: Node, maxlen: int = 160) -> list[tuple[str, str]]:
    """(label, value) pairs for the keys that matter on this node: sort/group/presorted/cache
    keys, scan filters, and how a sort/hash/aggregate actually ran."""
    p = n.props
    out: list[tuple[str, str]] = []

    def add(label: str, key: str) -> None:
        v = p.get(key)
        if v not in (None, "", []):
            out.append((label, _clip(_join_val(v), maxlen)))

    add("sort", "Sort Key")
    add("presorted", "Presorted Key")
    add("group", "Group Key")
    if p.get("Grouping Sets"):
        sets = []
        for gs in p["Grouping Sets"]:
            gk = gs.get("Group Key") if isinstance(gs, dict) else None
            if gk:
                sets.append("(" + _join_val(gk) + ")")
        if sets:
            out.append(("grouping sets", _clip(" ".join(sets), maxlen)))
    add("hash keys", "Hash Key")
    add("cache key", "Cache Key")
    add("order by", "Order By")
    if n.node_type not in JOINS:
        add("filter", "Filter")
    add("recheck", "Recheck Cond")
    # how it ran
    if p.get("Sort Method"):
        space = p.get("Sort Space Used")
        out.append(("sort ran", f"{p['Sort Method']}" + (f", {p.get('Sort Space Type', '')} {_kb(space)}" if space else "")))
    if p.get("Hash Batches") is not None:
        b = p["Hash Batches"]
        o = p.get("Original Hash Batches")
        out.append(("hash ran", f"{b} batch(es)" + (f" (planned {o})" if o not in (None, b) else "")
                    + (f", peak {_kb(p['Peak Memory Usage'])}" if p.get("Peak Memory Usage") else "")))
    if p.get("Disk Usage"):
        out.append(("agg spill", f"disk {_kb(p['Disk Usage'])}, {p.get('HashAgg Batches', '?')} batch(es)"))
    return out
