"""Small parser for the condition strings in plans ("Filter", "Index Cond", ...).

It only needs to answer: which columns of which table are compared how, so that
index / statistics candidates can be derived. Anything it cannot understand is
returned as an 'other' atom and ignored by the generators.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

BENIGN_CASTS = {"text", "character varying", "varchar", "bpchar", "character", "name"}
_OPS = ["IS NOT NULL", "IS NULL", "!~~*", "!~~", "~~*", "~~", "<>", "!=", "<=", ">=", "=", "<", ">"]
_IDENT = r'(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*)'


@dataclass
class Atom:
    text: str
    kind: str                       # eq | in | range | null | like_prefix | like_infix | join_eq | join_range | or | other
    col: str | None = None          # plain column name
    alias: str | None = None        # qualifier written in the plan, if any
    op: str | None = None
    func: str | None = None         # function wrapped around the column, e.g. 'lower'
    expr: str | None = None         # normalised expression for expression indexes / statistics
    cast: str | None = None         # type the column is cast to, if semantically meaningful
    refs: set[str] = field(default_factory=set)   # other aliases referenced on the right side


def _strip_parens(s: str) -> str:
    s = s.strip()
    while s.startswith("(") and s.endswith(")") and _balanced_outer(s):
        s = s[1:-1].strip()
    return s


def _balanced_outer(s: str) -> bool:
    depth, q = 0, None
    for i, ch in enumerate(s):
        if q:
            if ch == q:
                q = None
            continue
        if ch in "'\"":
            q = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and i != len(s) - 1:
                return False
    return depth == 0


def _split_top(s: str, word: str) -> list[str]:
    """Split on a top-level keyword (AND / OR), respecting parens and quotes."""
    parts, depth, q, last = [], 0, None, 0
    pat = f" {word} "
    i = 0
    while i < len(s):
        ch = s[i]
        if q:
            if ch == q:
                q = None
        elif ch in "'\"":
            q = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and s.startswith(pat, i):
            parts.append(s[last:i])
            i += len(pat)
            last = i
            continue
        i += 1
    parts.append(s[last:])
    return [p.strip() for p in parts if p.strip()]


def _find_op(s: str) -> tuple[int, str] | None:
    depth, q = 0, None
    i = 0
    while i < len(s):
        ch = s[i]
        if q:
            if ch == q:
                q = None
        elif ch in "'\"":
            q = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and ch in "=<>!~I" and (i == 0 or s[i - 1] == " " or s[i - 1] == ")"):
            for op in _OPS:
                if s.startswith(op, i) and (not op[0].isalpha() or s.startswith(op, i)):
                    # alphabetic operators need a word boundary on the left
                    return i, op
        i += 1
    return None


def split_conditions(cond: str | list[str] | None) -> list[str]:
    """Top-level AND terms (an OR at top level is kept as one term)."""
    if not cond:
        return []
    items = cond if isinstance(cond, list) else [cond]
    out: list[str] = []
    for c in items:
        c = _strip_parens(str(c))
        ors = _split_top(c, "OR")
        if len(ors) > 1:
            out.append(c)
            continue
        for t in _split_top(c, "AND"):
            t = _strip_parens(t)
            sub = _split_top(t, "AND")
            if len(sub) > 1:
                out.extend(split_conditions(t))
            else:
                out.append(t)
    return out


def _col_of(lhs: str) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """Return (alias, col, func, cast, expr) for the left operand."""
    s = _strip_parens(lhs)
    cast = None
    # peel trailing ::type casts on a bare column: ((col)::numeric)  /  (col)::text
    while True:
        m = re.match(r"^(.*)\)::([A-Za-z_ ]+?)(?:\[\])?$", s) or re.match(r"^(.*?)::([A-Za-z_ ]+?)(?:\[\])?$", s)
        if not m:
            break
        inner, typ = m.group(1), m.group(2).strip()
        if inner.count("(") > inner.count(")"):
            inner += ")"
        if not re.fullmatch(r"\(*" + _IDENT + r"(?:\." + _IDENT + r")?\)*", inner.strip()):
            break
        if typ not in BENIGN_CASTS:
            cast = typ
        s = _strip_parens(inner)
    m = re.fullmatch(r"(?:(" + _IDENT + r")\.)?(" + _IDENT + r")", s)
    if m:
        col = m.group(2).strip('"') if m.group(2).startswith('"') else m.group(2)
        expr = f"({col})::{cast}" if cast else None
        return (m.group(1), col, None, cast, expr)
    fm = re.fullmatch(r"(" + _IDENT + r")\((.*)\)", s)
    if fm:
        args = fm.group(2)
        cols = [x for x in re.findall(r"(?<![\w'\"])(?:(?:" + _IDENT + r")\.)?(" + _IDENT + r")(?![\w(])", re.sub(r"'(?:[^']|'')*'(?:::[A-Za-z_ ]+)?", "", args))
                if x.upper() not in {"TEXT", "DATE", "TIMESTAMP", "CHARACTER", "VARYING", "WITHOUT", "WITH", "TIME", "ZONE", "INTEGER", "NUMERIC", "BIGINT", "BOOLEAN"}]
        if cols:
            norm = re.sub(r"::(?:text|character varying|bpchar|name)", "", s)
            norm = re.sub(r"\(([A-Za-z_]\w*)\)", r"\1", norm)
            am = re.search(r"(?<![\w.])(" + _IDENT + r")\.(" + _IDENT + r")", args)
            return (am.group(1) if am else None, cols[0], fm.group(1), None, norm)
    return (None, None, None, None, None)


def parse_atoms(cond, known_aliases: set[str] | None = None) -> list[Atom]:
    known = known_aliases or set()
    atoms: list[Atom] = []
    for term in split_conditions(cond):
        if len(_split_top(term, "OR")) > 1:
            atoms.append(Atom(term, "or"))
            continue
        found = _find_op(term)
        if not found:
            atoms.append(Atom(term, "other"))
            continue
        pos, op = found
        lhs, rhs = term[:pos].strip(), term[pos + len(op):].strip()
        alias, col, func, cast, expr = _col_of(lhs)
        refs = {a for a in re.findall(r"(?<![\w.'])(" + _IDENT + r")\.(?:" + _IDENT + r")", rhs) if a in known and a != alias}
        # `col = ANY ('{..}')` shows up as "= ANY"
        is_any = op == "=" and rhs.startswith("ANY")
        if col is None:
            atoms.append(Atom(term, "other", op=op, refs=refs))
            continue
        if op in ("=",) and not refs:
            kind = "in" if is_any else "eq"
        elif op == "=" and refs:
            kind = "join_eq"
        elif op in ("<", ">", "<=", ">="):
            kind = "join_range" if refs else "range"
        elif op in ("IS NULL",):
            kind = "null"
        elif op in ("~~", "~~*"):
            kind = "like_infix" if re.match(r"^'%", rhs) or re.match(r"^'_", rhs) else "like_prefix"
        else:
            kind = "other"
        atoms.append(Atom(term, kind, col, alias, op, func, expr, cast, refs))
    return atoms


def own_atoms(atoms: list[Atom], own_names: set[str]) -> list[Atom]:
    """Atoms whose column belongs to the scanned relation (unqualified, or qualified with it)."""
    return [a for a in atoms if a.col and (a.alias is None or a.alias in own_names)]


def index_plan(atoms: list[Atom], max_cols: int = 3) -> dict:
    """Derive a btree column list: equality-like columns first, then one range column."""
    eq, rng, exprs, trgm, notes = [], [], [], [], []
    for a in atoms:
        if a.func or a.cast:
            e = a.expr
            if e and e not in exprs and a.kind in ("eq", "in", "join_eq", "range", "join_range"):
                exprs.append(e)
            if a.cast:
                notes.append(f"`{a.text}` casts column {a.col} to {a.cast}; if the column is not already {a.cast} "
                             "the plain index cannot be used - compare against a constant of the column's own type, "
                             "or index the cast expression.")
            if a.func:
                notes.append(f"`{a.text}` applies {a.func}() to column {a.col}; a plain index on {a.col} cannot be "
                             f"used - use an expression index.")
            continue
        if a.kind in ("eq", "in", "join_eq", "null") and a.col not in eq:
            eq.append(a.col)
        elif a.kind in ("range", "join_range") and a.col not in rng:
            rng.append(a.col)
        elif a.kind == "like_infix" and a.col not in trgm:
            trgm.append(a.col)
        elif a.kind == "like_prefix" and a.col not in eq:
            notes.append(f"LIKE '<prefix>%' on {a.col} needs a text_pattern_ops index (or a C-collation column) "
                         "to use a btree.")
        elif a.kind == "or":
            notes.append("An OR across conditions cannot use one btree range; index each branch separately "
                         "(the planner can BitmapOr them) or rewrite as UNION ALL.")
    cols = eq[:max_cols]
    if rng and len(cols) < max_cols:
        # only one range column is useful, and it must be last
        cols.append(next((r for r in rng if r not in cols), rng[0]))
    return {"columns": [c for c in cols], "equality": eq, "range": rng, "expressions": exprs,
            "trigram": trgm, "notes": list(dict.fromkeys(notes))}
