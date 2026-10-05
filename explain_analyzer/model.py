"""Data model: a parsed plan is a tree of Node objects wrapped in a Plan."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

SEVERITY_NAMES = {1: "info", 2: "warning", 3: "critical"}
SEVERITY_VALUES = {v: k for k, v in SEVERITY_NAMES.items()}


class Node:
    """One plan node. All EXPLAIN properties live in ``props`` using the
    key names of FORMAT JSON ("Node Type", "Actual Rows", ...), regardless of
    whether the plan was parsed from JSON or text. Derived values live in ``m``.
    """

    def __init__(self, props: dict[str, Any], parent: "Node | None" = None):
        self.id = -1
        self.props = props
        self.parent = parent
        self.children: list[Node] = []
        self.m: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.props.get(key, default)

    @property
    def node_type(self) -> str:
        return self.props.get("Node Type", "?")

    @property
    def relationship(self) -> str | None:
        return self.props.get("Parent Relationship")

    @property
    def subplan_name(self) -> str:
        return self.props.get("Subplan Name") or ""

    def label(self) -> str:
        p = self.props
        t = self.node_type
        if p.get("Node Type") == "Aggregate":
            strat = p.get("Strategy")
            t = {"Hashed": "HashAggregate", "Sorted": "GroupAggregate",
                 "Mixed": "MixedAggregate"}.get(strat, "Aggregate")
        if t == "ModifyTable" and p.get("Operation"):
            t = p["Operation"]
        if t in ("Index Scan", "Index Only Scan") and p.get("Scan Direction") == "Backward":
            t += " Backward"
        jt = p.get("Join Type")
        if jt and jt != "Inner" and "Join" in t:
            t = t.replace(" Join", f" {jt} Join")
        elif jt and jt != "Inner" and t == "Nested Loop":
            t = f"Nested Loop {jt} Join"
        if p.get("Partial Mode") in ("Partial", "Finalize"):
            t = f"{p['Partial Mode']} {t}"
        if p.get("Parallel Aware") and t.endswith(("Scan", "Join", "Hash", "Backward", "Aggregate")):
            t = f"Parallel {t}"
        out = t
        if p.get("Index Name") and t.startswith(("Index", "Parallel Index")):
            out += f" using {p['Index Name']}"
        if p.get("Index Name") and t.startswith("Bitmap Index"):
            out += f" on {p['Index Name']}"
        rel = p.get("Relation Name") or p.get("CTE Name") or p.get("Function Name")
        if rel:
            sch = p.get("Schema")
            out += " on " + (f"{sch}." if sch and p.get("Relation Name") else "") + str(rel)
            alias = p.get("Alias")
            if alias and alias != rel:
                out += f" {alias}"
        return out

    def walk(self) -> Iterator["Node"]:
        yield self
        for c in self.children:
            yield from c.walk()

    def ancestors(self) -> Iterator["Node"]:
        n = self.parent
        while n is not None:
            yield n
            n = n.parent

    def is_subplan_child(self) -> bool:
        return self.relationship in ("InitPlan", "SubPlan")


@dataclass
class Finding:
    rule: str
    severity: int
    title: str
    detail: str = ""
    suggestion: str = ""
    node_id: int | None = None
    impact_ms: float = 0.0
    context: list[str] = field(default_factory=list)  # where in the query this applies

    @property
    def severity_name(self) -> str:
        return SEVERITY_NAMES[self.severity]


@dataclass
class Plan:
    root: Node
    planning_time: float | None = None
    execution_time: float | None = None
    triggers: list[dict[str, Any]] = field(default_factory=list)
    jit: dict[str, Any] | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    planning_buffers: dict[str, Any] = field(default_factory=dict)
    query: str | None = None
    nodes: list[Node] = field(default_factory=list)
    m: dict[str, Any] = field(default_factory=dict)

    def index(self) -> None:
        self.nodes = list(self.root.walk())
        for i, n in enumerate(self.nodes, 1):
            n.id = i

    @property
    def has_analyze(self) -> bool:
        return self.root.get("Actual Total Time") is not None or \
            self.root.get("Actual Rows") is not None
