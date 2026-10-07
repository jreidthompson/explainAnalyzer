"""Terminal report: summary, ranked findings, annotated plan tree."""
from __future__ import annotations

from .context import join_on, join_sides, keys
from .model import Finding, Node, Plan, SEVERITY_NAMES

_COL = {1: "36", 2: "33", 3: "1;31"}
_MARK = {1: "i", 2: "!", 3: "X"}


def _c(text: str, code: str, on: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if on else text


def _bar(pct: float, width: int = 10) -> str:
    filled = min(width, int(round(pct / 100 * width)))
    return "#" * filled + "." * (width - filled)


def summary_lines(plan: Plan) -> list[str]:
    out = []
    if plan.planning_time is not None:
        out.append(f"Planning time:  {plan.planning_time:,.3f} ms")
    if plan.execution_time is not None:
        out.append(f"Execution time: {plan.execution_time:,.3f} ms")
    if plan.jit and (plan.jit.get("Timing") or {}).get("Total") is not None:
        out.append(f"JIT:            {plan.jit['Timing']['Total']:,.3f} ms")
    for t in plan.triggers:
        out.append(f"Trigger {t.get('Trigger Name')}: {t.get('Time')} ms ({t.get('Calls')} calls)")
    tot = plan.m.get("buf_total") or {}
    if tot:
        out.append("Buffers (root): " + ", ".join(f"{k.replace(' Blocks', '').lower()}={v:,}" for k, v in tot.items() if v))
    if not plan.m["analyzed"]:
        out.append("NOTE: no ANALYZE data - only cost-based checks were possible.")
    bare = sorted({n.get("Relation Name") for n in plan.nodes if n.get("Relation Name") and not n.get("Schema")})
    if bare:
        shown = ", ".join(bare[:5]) + (f" (+{len(bare) - 5} more)" if len(bare) > 5 else "")
        out.append(f"NOTE: the plan names no schema for: {shown}. EXPLAIN omits the schema of tables on the search_path; "
                   "capture with EXPLAIN (ANALYZE, BUFFERS, VERBOSE, ...) so every table is schema.table, "
                   "or pass --schema NAME.")
    return out


def render(plan: Plan, findings: list[Finding], color: bool = False, top: int = 0,
           min_severity: int = 1, actions: bool = True) -> str:
    L: list[str] = []
    L.append(_c("== Summary ==", "1", color))
    L.extend(summary_lines(plan))
    by_node: dict[int, int] = {}
    for f in findings:
        if f.node_id is not None:
            by_node[f.node_id] = max(by_node.get(f.node_id, 0), f.severity)

    shown = [f for f in findings if f.severity >= min_severity]
    if top:
        shown = shown[:top]
    L.append("")
    counts = {s: sum(1 for f in findings if f.severity == s) for s in (3, 2, 1)}
    L.append(_c(f"== Findings ({counts[3]} critical, {counts[2]} warning, {counts[1]} info) ==", "1", color))
    if not shown:
        L.append("No problems detected.")
    for i, f in enumerate(shown, 1):
        tag = _c(f"[{SEVERITY_NAMES[f.severity].upper()}]", _COL[f.severity], color)
        loc = f" (node #{f.node_id})" if f.node_id else ""
        L.append(f"{i:>2}. {tag} {f.title}{loc}")
        if f.detail:
            L.append(f"      {f.detail}")
        for c in f.context:
            L.append(_c(f"      | {c}", "2", color))
        if f.suggestion:
            L.append(f"      -> {f.suggestion}")
        if f.actions:
            L.append(f"      fix: see action {', '.join(f.actions)} below")

    L.append("")
    L.append(_c("== Plan (excl = own time, rows = estimated -> actual per loop) ==", "1", color))
    _tree(plan.root, L, "", True, plan, by_node, color)
    if actions and plan.m.get("actions") is not None:
        L.append("")
        L.append(render_actions(plan.m["actions"], color))
    return "\n".join(L)


def _node_line(n: Node, plan: Plan, sev: int, color: bool) -> str:
    m = n.m
    mark = _c(f"[{_MARK[sev]}]", _COL[sev], color) if sev else "   "
    if m["never"]:
        stats = "never executed"
    elif plan.m["analyzed"]:
        extra = f" x{m['mis']:,.0f} {m['mis_dir']}" if m["mis"] >= 10 and not m["mis_ignored"] else ""
        stats = (f"excl {m['excl_ms']:>9,.2f} ms {_bar(m['excl_pct'])} {m['excl_pct']:>3.0f}%  "
                 f"rows {m['est_rows']:,.0f} -> {m['act_rows']:,.0f}{extra}  loops {m['loops']:,}")
    else:
        stats = f"cost {n.get('Startup Cost')}..{n.get('Total Cost')} rows {m['est_rows']:,.0f} ({m['cost_pct']:.0f}% own cost)"
    return f"{mark} #{n.id} {n.label()}  {stats}"


def _tree(n: Node, out: list[str], prefix: str, last: bool, plan: Plan,
          by_node: dict[int, int], color: bool, root: bool = True) -> None:
    sev = by_node.get(n.id, 0)
    line = _node_line(n, plan, sev, color)
    if n is plan.root:
        out.append(line)
        child_prefix = "    "
    else:
        out.append(f"{prefix}{'`-' if last else '|-'}{line}")
        child_prefix = prefix + ("  " if last else "| ")
    on, sides = join_on(n), join_sides(n)
    if on:
        out.append(_c(f"{child_prefix}      on: {on}", "2", color))
    if sides:
        out.append(_c(f"{child_prefix}      tables: {sides}", "2", color))
    for label, val in keys(n):
        out.append(_c(f"{child_prefix}      {label}: {val}", "2", color))
    for i, c in enumerate(n.children):
        crel = c.get("Subplan Name") or ""
        if crel:
            out.append(f"{child_prefix}   ({crel})")
        _tree(c, out, child_prefix, i == len(n.children) - 1, plan, by_node, color, False)


def render_actions(actions, color: bool = False, with_sql: bool = True) -> str:
    from .remedies import STAGES
    L = [_c("== Action plan (work top to bottom; re-capture the plan after each stage) ==", "1", color)]
    if not actions:
        L.append("Nothing actionable was derived from the findings.")
        return "\n".join(L)
    L.append("Why this order: refresh statistics first (cheap, and it can make later findings vanish), then structural "
             "fixes, then settings. Each change can alter the plan, so findings below a change may no longer apply.")
    stage = None
    for a in actions:
        if a.stage != stage:
            stage = a.stage
            L += ["", _c(f"-- Stage {stage}: {STAGES[stage]}", "1;36", color)]
        flags = [a.kind, f"confidence {a.confidence}"]
        if a.impact_ms:
            flags.append(f"~{a.impact_ms:,.0f} ms at stake")
        if a.diagnostic_only:
            flags.append("DIAGNOSTIC ONLY")
        if a.manual:
            flags.append("manual change")
        L.append(f"{_c(a.id, '1', color)}  {a.title}   [{', '.join(flags)}]")
        L.append(f"      why: {a.why}")
        if with_sql:
            for label, stmts in a.try_variants:
                head = "try (safe: run inside BEGIN ... ROLLBACK)" if a.transactional else "try (NOT rolled back - routine maintenance)"
                L.append(f"      {head}: {label}")
                L += [f"          {s}" for s in stmts]
            if a.apply:
                L.append("      apply:")
                L += [f"          {s}" for s in a.apply]
            if a.investigate_sql:
                L.append("      run / read:")
                for q in a.investigate_sql:
                    L += [f"          {line}" for line in q.split("\n")]
        for c in a.caveats:
            L.append(f"      caution: {c}")
        if a.verify:
            L.append(f"      verify: {a.verify}")
    return "\n".join(L)
