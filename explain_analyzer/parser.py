"""Parse EXPLAIN output (FORMAT JSON or default text) into Plan objects.

Text plans are normalised to the same property names FORMAT JSON uses so that
every later stage only deals with one representation.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .model import Node, Plan

BUFFER_KEYS = {
    "hit": "Hit Blocks", "read": "Read Blocks",
    "dirtied": "Dirtied Blocks", "written": "Written Blocks",
}


class ParseError(ValueError):
    pass


# ---------------------------------------------------------------- input cleanup

_PSQL_NOISE = re.compile(r"^\s*(-{3,}[-+]*|QUERY PLAN|\(\d+ rows?\))\s*$")


def _clean(text: str) -> str:
    lines = text.replace("\r\n", "\n").split("\n")
    lines = [ln for ln in lines if not _PSQL_NOISE.match(ln)]
    # psql aligned output appends " +" to wrapped lines.
    nonempty = [ln for ln in lines if ln.strip()]
    if nonempty and sum(ln.rstrip().endswith(" +") for ln in nonempty) > len(nonempty) / 2:
        lines = [re.sub(r"\s\+\s*$", "", ln) for ln in lines]
    # auto_explain log prefix / query text
    out = []
    for ln in lines:
        if re.search(r"\bLOG:\s+duration:.*\bplan:\s*$", ln):
            continue
        if ln.lstrip().startswith("Query Text:"):
            continue
        out.append(ln)
    return "\n".join(out)


def parse_input(text: str) -> list[Plan]:
    """Parse a blob of EXPLAIN output; returns one Plan per statement."""
    text = _clean(text)
    stripped = text.lstrip()
    if not stripped:
        raise ParseError("empty input")
    if stripped[0] in "[{":
        plans = _parse_json_text(stripped)
    else:
        plans = parse_text(text)
    if not plans:
        raise ParseError("no plan found in input")
    for p in plans:
        p.index()
    return plans


# ------------------------------------------------------------------------ JSON

def _parse_json_text(s: str) -> list[Plan]:
    dec = json.JSONDecoder()
    plans: list[Plan] = []
    pos = 0
    while pos < len(s):
        while pos < len(s) and s[pos].isspace():
            pos += 1
        if pos >= len(s):
            break
        try:
            obj, end = dec.raw_decode(s, pos)
        except json.JSONDecodeError as e:
            raise ParseError(f"invalid JSON plan: {e}") from e
        pos = end
        items = obj if isinstance(obj, list) else [obj]
        for it in items:
            if isinstance(it, dict) and "Plan" in it:
                plans.append(_plan_from_json(it))
    return plans


def _node_from_json(d: dict[str, Any], parent: Node | None) -> Node:
    props = {k: v for k, v in d.items() if k != "Plans"}
    node = Node(props, parent)
    for c in d.get("Plans", []) or []:
        node.children.append(_node_from_json(c, node))
    return node


def _plan_from_json(d: dict[str, Any]) -> Plan:
    plan = Plan(root=_node_from_json(d["Plan"], None))
    plan.planning_time = d.get("Planning Time")
    plan.execution_time = d.get("Execution Time")
    plan.triggers = d.get("Triggers") or []
    plan.jit = d.get("JIT")
    plan.settings = d.get("Settings") or {}
    plan.planning_buffers = (d.get("Planning") or {}) if isinstance(d.get("Planning"), dict) else {}
    plan.query = d.get("Query Text")
    return plan


# ------------------------------------------------------------------------ text

_ARROW = re.compile(r"^(\s*)(->\s*)?(.*?)\s*$")
_COST = re.compile(r"\(cost=([\d.]+)\.\.([\d.]+) rows=(\d+) width=(\d+)\)")
_ACT = re.compile(r"\(actual (?:time=([\d.]+)\.\.([\d.]+) )?rows=([\d.]+) loops=(\d+)\)")
_LABEL = re.compile(r"^(InitPlan|SubPlan|CTE)\b[^:]*$")
_JOIN = re.compile(r"^(Nested Loop|Hash|Merge)(?: (Left|Right|Full|Semi|Anti|Right Semi|Right Anti))? Join$")
_AGG = {"Aggregate": "Plain", "HashAggregate": "Hashed",
        "GroupAggregate": "Sorted", "MixedAggregate": "Mixed"}
_SCAN = re.compile(
    r"^(?P<type>.+?)(?: using (?P<idx>\S+))?(?: on (?P<rel>\S+)(?: (?P<alias>\S+))?)?$")
_INT_KEYS = (
    "Rows Removed by Filter", "Rows Removed by Index Recheck", "Rows Removed by Join Filter",
    "Heap Fetches", "Workers Planned", "Workers Launched", "Index Searches",
    "Subplans Removed", "Full-sort Groups", "Pre-sorted Groups",
)
_NUM = r"(\d+(?:\.\d+)?)"


def _num(s: str) -> float | int:
    f = float(s)
    return int(f) if f.is_integer() and "." not in s else f


def _name_props(name: str) -> dict[str, Any]:
    props: dict[str, Any] = {}
    name = name.strip()
    m = re.match(r"^(Insert|Update|Delete|Merge) on (\S+)(?: (\S+))?$", name)
    if m:
        props.update({"Node Type": "ModifyTable", "Operation": m.group(1),
                      "Relation Name": m.group(2)})
        if m.group(3):
            props["Alias"] = m.group(3)
        return props
    m = _JOIN.match(name)
    if m:
        base, jt = m.group(1), m.group(2) or "Inner"
        props["Node Type"] = "Nested Loop" if base == "Nested Loop" else f"{base} Join"
        props["Join Type"] = jt
        return props
    if name == "Nested Loop":
        return {"Node Type": "Nested Loop", "Join Type": "Inner"}
    mode = None
    for pre in ("Partial ", "Finalize "):
        if name.startswith(pre):
            mode, name = pre.strip(), name[len(pre):]
    if name in _AGG:
        props.update({"Node Type": "Aggregate", "Strategy": _AGG[name],
                      "Partial Mode": mode or "Simple"})
        return props
    if mode:
        name = f"{mode} {name}"
    if name.startswith("Parallel "):
        props["Parallel Aware"] = True
        name = name[len("Parallel "):]
    m = _SCAN.match(name)
    t = m.group("type") if m else name
    if t.endswith(" Backward"):
        t = t[: -len(" Backward")]
        props["Scan Direction"] = "Backward"
    props["Node Type"] = t
    if m:
        if m.group("idx"):
            props["Index Name"] = m.group("idx")
        rel = m.group("rel")
        if rel:
            if t == "Bitmap Index Scan":
                props["Index Name"] = rel
            elif t == "CTE Scan":
                props["CTE Name"] = rel
                if m.group("alias"):
                    props["Alias"] = m.group("alias")
            elif t == "Function Scan":
                props["Function Name"] = rel
                if m.group("alias"):
                    props["Alias"] = m.group("alias")
            else:
                if "." in rel and not rel.startswith('"'):
                    sch, rel = rel.split(".", 1)
                    props["Schema"] = sch
                props["Relation Name"] = rel
                if m.group("alias"):
                    props["Alias"] = m.group("alias")
    return props


def _parse_buffers(rest: str, into: dict[str, Any]) -> None:
    for kind, body in re.findall(r"(shared|local|temp)\s+((?:\w+=\d+\s*)+)", rest):
        for k, v in re.findall(r"(\w+)=(\d+)", body):
            if k in BUFFER_KEYS:
                into[f"{kind.capitalize()} {BUFFER_KEYS[k]}"] = int(v)


def _parse_io_timings(rest: str, into: dict[str, Any]) -> None:
    # "I/O Timings: shared read=1.2 write=0.3, local ..." (PG17) or "read=1.2 write=.." (<=16)
    for kind, body in re.findall(r"(?:(shared|local|temp)\s+)?((?:(?:read|write)=[\d.]+\s*)+)", rest):
        for k, v in re.findall(r"(read|write)=([\d.]+)", body):
            into[(f"{kind.capitalize()} " if kind else "") + f"I/O {k.capitalize()} Time"] = float(v)


def _detail(node: Node, line: str) -> None:
    """Record a detail line (``Key: value``) on a node."""
    p = node.props
    s = line.strip()
    if re.match(r"^Worker \d+:", s):
        p.setdefault("Worker Details", []).append(s)
        return
    if s.startswith("Buffers:"):
        _parse_buffers(s, p)
        return
    if s.startswith("I/O Timings:"):
        _parse_io_timings(s, p)
        return
    m = re.match(r"^(?:Full-sort Groups: .*?)?Sort Method: (.+?)\s+(?:(?:Average|Peak)\s+)?(Memory|Disk): (\d+)kB", s)
    if m:
        method, typ, kb = m.group(1), m.group(2), int(m.group(3))
        if "external" in method or typ == "Disk":
            typ = "Disk"
        p["Sort Method"] = method
        p["Sort Space Type"] = typ
        p["Sort Space Used"] = kb
        return
    if s.startswith("Sort Method:"):
        p["Sort Method"] = s.split(":", 1)[1].strip()
        return
    m = re.match(rf"^Buckets: (\d+)(?: \(originally (\d+)\))?\s+Batches: (\d+)(?: \(originally (\d+)\))?\s+Memory Usage: (\d+)kB", s)
    if m:
        p["Hash Buckets"] = int(m.group(1))
        if m.group(2):
            p["Original Hash Buckets"] = int(m.group(2))
        p["Hash Batches"] = int(m.group(3))
        if m.group(4):
            p["Original Hash Batches"] = int(m.group(4))
        p["Peak Memory Usage"] = int(m.group(5))
        return
    m = re.match(r"^Batches: (\d+)\s+Memory Usage: (\d+)kB(?:\s+Disk Usage: (\d+)kB)?", s)
    if m:  # HashAggregate
        p["HashAgg Batches"] = int(m.group(1))
        p["Peak Memory Usage"] = int(m.group(2))
        if m.group(3):
            p["Disk Usage"] = int(m.group(3))
        return
    m = re.match(r"^Hits: (\d+)\s+Misses: (\d+)\s+Evictions: (\d+)\s+Overflows: (\d+)\s+Memory Usage: (\d+)kB", s)
    if m:  # Memoize
        p.update({"Cache Hits": int(m.group(1)), "Cache Misses": int(m.group(2)),
                  "Cache Evictions": int(m.group(3)), "Cache Overflows": int(m.group(4)),
                  "Peak Memory Usage": int(m.group(5))})
        return
    m = re.match(r"^Heap Blocks: (.*)$", s)
    if m:
        for k, v in re.findall(r"(exact|lossy)=(\d+)", m.group(1)):
            p[f"{k.capitalize()} Heap Blocks"] = int(v)
        return
    m = re.match(r"^([A-Za-z][A-Za-z \-/]*?): (.*)$", s)
    if not m:
        return
    key, val = m.group(1), m.group(2).strip()
    if key in _INT_KEYS and re.fullmatch(_NUM, val):
        p[key] = _num(val)
    elif key == "Planned Partitions":
        p[key] = val
    else:
        p[key] = val


def parse_text(text: str) -> list[Plan]:
    plans: list[Plan] = []
    plan: Plan | None = None
    stack: list[tuple[int, Node]] = []  # (text column, node)
    pending_label: tuple[int, str] | None = None
    section: str | None = None
    finished = False  # saw Planning/Execution Time for the current plan
    jit_lines: list[str] = []

    def flush_jit() -> None:
        if plan is not None and jit_lines:
            plan.jit = _parse_jit(jit_lines)
        jit_lines.clear()

    for raw in text.split("\n"):
        if not raw.strip():
            continue
        s = raw.strip()

        # ---- top-level summary lines
        m = re.match(rf"^Planning Time: {_NUM} ms", s)
        if m and plan:
            plan.planning_time = float(m.group(1)); finished = True; section = None; flush_jit(); continue
        m = re.match(rf"^Execution Time: {_NUM} ms", s)
        if m and plan:
            plan.execution_time = float(m.group(1)); finished = True; section = None; flush_jit(); continue
        if plan and s == "Planning:":
            section = "planning"; continue
        if plan and s == "JIT:":
            section = "jit"; continue
        if plan and s.startswith("Settings:"):
            for k, v in re.findall(r"(\w+) = '([^']*)'", s):
                plan.settings[k] = v
            section = None
            continue
        m = re.match(rf"^Trigger (.+?): time={_NUM} calls=(\d+)", s)
        if m and plan:
            plan.triggers.append({"Trigger Name": m.group(1), "Time": float(m.group(2)),
                                  "Calls": int(m.group(3))})
            continue
        if section == "planning" and plan:
            if s.startswith("Buffers:"):
                _parse_buffers(s, plan.planning_buffers)
            continue
        if section == "jit":
            jit_lines.append(s)
            continue

        # ---- plan tree lines
        am = _ARROW.match(raw)
        indent, arrow, body = len(am.group(1)), am.group(2), am.group(3)
        col = indent + len(arrow or "")
        is_node = bool(arrow) or bool(_COST.search(body))
        if not is_node and plan is None and not _LABEL.match(body) and ":" not in body.split("(")[0]:
            is_node = True  # root of a COSTS OFF plan
        if not is_node and finished and plan is not None and indent == 0 and _COST.search(body):
            is_node = True

        if is_node:
            if finished and not arrow:  # a second plan begins
                flush_jit()
                plan = None; stack = []; finished = False
            hm = _COST.search(body)
            name = body[: hm.start()].strip() if hm else re.sub(r"\s*\((actual|never).*$", "", body).strip()
            props = _name_props(name)
            if hm:
                props["Startup Cost"] = float(hm.group(1)); props["Total Cost"] = float(hm.group(2))
                props["Plan Rows"] = int(hm.group(3)); props["Plan Width"] = int(hm.group(4))
            am2 = _ACT.search(body)
            if am2:
                if am2.group(1) is not None:
                    props["Actual Startup Time"] = float(am2.group(1))
                    props["Actual Total Time"] = float(am2.group(2))
                props["Actual Rows"] = _num(am2.group(3))
                props["Actual Loops"] = int(am2.group(4))
            elif "(never executed)" in body:
                props["Actual Loops"] = 0
                props["Actual Rows"] = 0
                props["Actual Total Time"] = 0.0
                props["Actual Startup Time"] = 0.0
                props["Never Executed"] = True
            while stack and stack[-1][0] >= col:
                stack.pop()
            parent = stack[-1][1] if stack else None
            if pending_label and (parent is not None):
                lab = pending_label[1]
                props["Subplan Name"] = lab
                props["Parent Relationship"] = "SubPlan" if lab.startswith("SubPlan") else "InitPlan"
                pending_label = None
            elif parent is not None:
                seen = sum(1 for c in parent.children if c.relationship in ("Outer", "Inner", "Member"))
                props["Parent Relationship"] = "Outer" if seen == 0 else "Inner"
            node = Node(props, parent)
            if plan is None:
                plan = Plan(root=node)
                plans.append(plan)
                section = None
            if parent is not None:
                parent.children.append(node)
            stack.append((col, node))
            continue

        if _LABEL.match(body) and plan is not None:
            pending_label = (indent, body)
            continue

        # ---- detail line: belongs to the nearest node left of this indent
        if plan is None:
            continue
        while stack and stack[-1][0] >= indent and len(stack) > 1:
            stack.pop()
        if stack:
            _detail(stack[-1][1], s)
    flush_jit()
    return plans


def _parse_jit(lines: list[str]) -> dict[str, Any]:
    jit: dict[str, Any] = {}
    for s in lines:
        m = re.match(r"^Functions: (\d+)", s)
        if m:
            jit["Functions"] = int(m.group(1))
        if s.startswith("Options:"):
            jit["Options"] = {k: v == "true" for k, v in
                              re.findall(r"(\w+) (true|false)", s)}
        if s.startswith("Timing:"):
            jit["Timing"] = {k: float(v) for k, v in
                             re.findall(rf"(Generation|Inlining|Optimization|Emission|Total) {_NUM} ms", s)}
    return jit
