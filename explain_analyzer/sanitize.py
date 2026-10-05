"""Best-effort redaction so reports can be shared. Review output before sharing."""
from __future__ import annotations

import hashlib
import re

from .model import Plan

EXPR_KEYS = {
    "Filter", "Index Cond", "Recheck Cond", "Hash Cond", "Merge Cond", "Join Filter",
    "One-Time Filter", "TID Cond", "Order By", "Output", "Group Key", "Sort Key",
    "Presorted Key", "Cache Key", "Cache Mode", "Function Call", "Conflict Filter",
    "Worker Details", "Table Function Name",
}
NAME_KEYS = {"Relation Name", "Index Name", "Alias", "Schema", "CTE Name", "Function Name",
             "Trigger Name", "Constraint Name", "Subplan Name", "Relation", "Name"}

KEEP = {w.lower() for w in """
and or not is null true false any all array in like ilike between case when then else end desc asc
nulls first last text integer int int4 int8 bigint smallint numeric date timestamp time timestamptz
with without zone character varying boolean bool uuid double precision interval jsonb json bytea
real float4 float8 name regclass oid inet cidr bpchar varchar char initplan subplan hashed
returns similar to escape distinct filter over partition by row rows unbounded preceding following
current from for select as on using inner left right full outer semi anti cross
""".split()}

_TOKEN = re.compile(r"""
    (?P<str>'(?:[^']|'')*')
  | (?P<qid>"(?:[^"]|"")+")
  | (?P<num>\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b)
  | (?P<id>[A-Za-z_][A-Za-z0-9_$]*)
""", re.X)


def _h(prefix: str, s: str, salt: str) -> str:
    return prefix + hashlib.sha256((salt + s).encode()).hexdigest()[:6]


class Sanitizer:
    def __init__(self, names: bool = True, salt: str = ""):
        self.names = names
        self.salt = salt

    def name(self, s: str) -> str:
        if not self.names or not isinstance(s, str):
            return s
        if s.startswith(("InitPlan", "SubPlan")) or s == "CTE":
            return s
        if s.startswith("CTE "):
            return "CTE " + self.name(s[4:])
        return ".".join(_h("n_", part.strip('"'), self.salt) for part in s.split("."))

    def expr(self, s: str) -> str:
        def repl(m: re.Match) -> str:
            if m.group("str"):
                return "'?'"
            if m.group("num"):
                return "?"
            tok = m.group(0)
            if not self.names:
                return tok
            if m.group("id"):
                before = s[: m.start()]
                after = s[m.end():]
                if tok.lower() in KEEP or after.startswith("(") or before.rstrip().endswith("::") \
                        or re.match(r"^\(InitPlan", s[m.start() - 1:]) or tok.startswith("n_"):
                    return tok
            return _h("c_", tok.strip('"'), self.salt)
        return _TOKEN.sub(repl, s)

    def plan(self, plan: Plan) -> None:
        for n in plan.nodes:
            for k, v in list(n.props.items()):
                if k in EXPR_KEYS:
                    n.props[k] = [self.expr(x) for x in v] if isinstance(v, list) else self.expr(v)
                elif k in NAME_KEYS and isinstance(v, str):
                    n.props[k] = self.name(v)
        for t in plan.triggers:
            for k in ("Trigger Name", "Constraint Name", "Relation"):
                if k in t:
                    t[k] = self.name(t[k])
        plan.query = None
