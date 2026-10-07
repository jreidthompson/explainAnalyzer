"""Split a SQL file into statements and separate the statement to EXPLAIN from its preamble.

A real query file often looks like ``SET work_mem = '6GB'; SET hash_mem_multiplier = 4; SELECT ...;``.
Only the last statement can follow EXPLAIN; everything before it is replayed as a preamble inside every
experiment transaction so the experiment runs under the same settings the plan was captured with.
"""
from __future__ import annotations

import re

_EXPLAIN_PREFIX = re.compile(
    r"^\s*EXPLAIN\s*(?:\((?:[^()]|\([^()]*\))*\)|(?:(?:ANALYZE|ANALYSE|VERBOSE)\s+)+)?\s*", re.I)
_TXN_CONTROL = re.compile(r"^\s*(BEGIN|START\s+TRANSACTION|COMMIT|END|ROLLBACK|ABORT)\b", re.I)
_SET = re.compile(r"^\s*(?:SET|RESET)\s+(?:(?:SESSION|LOCAL)\s+)?([A-Za-z_][\w.]*)", re.I)


def split_statements(sql: str) -> list[str]:
    """Split on top-level semicolons, honouring '...', "...", $tag$...$tag$, -- and /* */ comments."""
    out, buf, i, n = [], [], 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "'" or c == '"':
            j = i + 1
            while j < n:
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:
                        j += 2
                        continue
                    break
                j += 1
            buf.append(sql[i:j + 1]); i = j + 1
        elif c == "$":
            m = re.match(r"\$([A-Za-z_]\w*)?\$", sql[i:])
            if m:
                tag = m.group(0)
                end = sql.find(tag, i + len(tag))
                end = n if end < 0 else end + len(tag)
                buf.append(sql[i:end]); i = end
            else:
                buf.append(c); i += 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            buf.append(sql[i:j]); i = j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            j = n if j < 0 else j + 2
            buf.append(sql[i:j]); i = j
        elif c == ";":
            out.append("".join(buf)); buf = []; i += 1
        else:
            buf.append(c); i += 1
    out.append("".join(buf))
    return [s.strip() for s in out if _has_sql(s)]


def _code(s: str) -> str:
    """Statement text with leading comments removed (for matching keywords)."""
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r"^\s*--[^\n]*", "", s)
        s = re.sub(r"^\s*/\*.*?\*/", "", s, flags=re.S)
    return s.strip()


def _has_sql(s: str) -> bool:
    """False for empty strings and comment-only fragments."""
    s = re.sub(r"--[^\n]*", "", s)
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return bool(s.strip())


def prepare(text: str) -> dict:
    """Return {'main', 'preamble', 'notes', 'set_names'} for a query file."""
    notes: list[str] = []
    lines = []
    for ln in text.splitlines():
        if ln.lstrip().startswith("\\"):
            notes.append(f"ignored psql meta-command in the query file: {ln.strip()}")
        else:
            lines.append(ln)
    stmts = split_statements("\n".join(lines))
    if not stmts:
        raise ValueError("the query file contains no SQL statement")
    main = stmts[-1]
    pre = []
    for s in stmts[:-1]:
        if _TXN_CONTROL.match(_code(s)):
            notes.append(f"dropped '{_code(s).split()[0].upper()}' from the preamble: every experiment manages its own transaction")
        else:
            pre.append(s)
    if _TXN_CONTROL.match(_code(main)):
        raise ValueError("the last statement in the query file is transaction control, not the query to analyze")
    code = _code(main)
    m = _EXPLAIN_PREFIX.match(code)
    if m and re.match(r"^\s*EXPLAIN\b", code, re.I):
        main = code[m.end():].strip()
        notes.append("removed a leading EXPLAIN from the query (the script adds its own)")
    if _SET.match(_code(main)):
        raise ValueError("the last statement is a SET command; put the query to analyze last in the file")
    if pre:
        notes.append(f"{len(pre)} statement(s) before the query will be replayed inside every experiment "
                     f"transaction (and rolled back with it): " + "; ".join(p.split("\n")[0][:50] for p in pre))
    names = {mm.group(1).lower() for s in pre for mm in [_SET.match(_code(s))] if mm}
    return {"main": main, "preamble": pre, "notes": notes, "set_names": names}
