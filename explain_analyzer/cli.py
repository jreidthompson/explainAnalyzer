"""Command line interface."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .metrics import Config, compute
from .model import SEVERITY_VALUES, Plan, Finding
from .parser import ParseError, parse_input
from .remedies import build_actions
from .rules import run_rules
from .sanitize import Sanitizer


def analyze_text(text: str, cfg: Config | None = None, sanitize: str | None = None,
                 salt: str = "", schema: str | None = None) -> list[tuple[Plan, list[Finding]]]:
    """Parse + analyse. ``sanitize`` is None, 'literals' or 'names'."""
    cfg = cfg or Config()
    out = []
    for plan in parse_input(text):
        compute(plan, cfg)
        if sanitize:
            Sanitizer(names=sanitize == "names", salt=salt).plan(plan)
        findings = run_rules(plan, cfg)
        plan.m["actions"] = build_actions(plan, findings, cfg, schema)
        out.append((plan, findings))
    return out


def fingerprint(plan: Plan) -> dict:
    """What a faithful re-run of this query must look like (used by --compare to detect a wrong-table baseline)."""
    r = plan.root.m
    return {"labels": [n.label() for n in plan.nodes],
            "rows": r.get("act_rows") if r.get("act_rows") is not None else r.get("est_rows"),
            "est_rows": r.get("est_rows"),
            "scan_rows": {n.label(): (n.m["act_rows"] if n.m.get("act_rows") is not None else n.m["est_rows"])
                          for n in plan.nodes if n.get("Relation Name")}}


def _json_doc(plan: Plan, findings: list[Finding]) -> dict:
    return {
        "planning_time_ms": plan.planning_time,
        "execution_time_ms": plan.execution_time,
        "analyzed": plan.m["analyzed"],
        "findings": [{"rule": f.rule, "severity": f.severity_name, "title": f.title,
                      "detail": f.detail, "suggestion": f.suggestion, "node": f.node_id,
                      "context": f.context, "actions": f.actions,
                      "impact_ms": round(f.impact_ms, 3)} for f in findings],
        "actions": [{"id": a.id, "kind": a.kind, "stage": a.stage, "title": a.title, "why": a.why,
                     "confidence": a.confidence, "impact_ms": round(a.impact_ms, 1),
                     "try": [{"label": l, "sql": s} for l, s in a.try_variants], "apply": a.apply,
                     "investigate_sql": a.investigate_sql, "caveats": a.caveats, "verify": a.verify,
                     "diagnostic_only": a.diagnostic_only, "manual": a.manual, "nodes": a.nodes}
                    for a in plan.m.get("actions", [])],
        "nodes": [{"id": n.id, "parent": n.parent.id if n.parent else None, "label": n.label(),
                   "exclusive_ms": round(n.m["excl_ms"], 3), "exclusive_pct": round(n.m["excl_pct"], 1),
                   "loops": n.m["loops"], "est_rows": n.m["est_rows"], "actual_rows": n.m["act_rows"],
                   "misestimate": round(n.m["mis"], 2)} for n in plan.nodes],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="explain-analyzer",
        description="Analyse PostgreSQL EXPLAIN plans (JSON or text) locally; no network access.")
    ap.add_argument("file", nargs="?", default="-", help="plan file, or - for stdin (default)")
    ap.add_argument("--html", metavar="OUT", help="write a self-contained HTML report")
    ap.add_argument("--json", action="store_true", help="print machine-readable JSON instead of the text report")
    ap.add_argument("--top", type=int, default=0, help="show only the N most important findings")
    ap.add_argument("--min-severity", choices=list(SEVERITY_VALUES), default="info")
    ap.add_argument("--fail-on", choices=list(SEVERITY_VALUES),
                    help="exit with status 2 if a finding of at least this severity exists")
    ap.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    ap.add_argument("--sanitize", choices=["literals", "names"],
                    help="redact constants (literals) or constants and identifiers (names) in output")
    ap.add_argument("--salt", default="", help="salt for hashed identifiers with --sanitize names")
    ap.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                    help="override a rule threshold (see explain_analyzer/metrics.py Config)")
    ap.add_argument("--no-actions", action="store_true", help="omit the action plan from the text report")
    ap.add_argument("--script", metavar="DIR",
                    help="write DIR/experiments.sql: a psql script that tests every action inside "
                         "BEGIN...ROLLBACK and captures the resulting plans")
    ap.add_argument("--schema", metavar="NAME",
                    help="schema of the tables whose plan shows none (EXPLAIN omits the schema for tables on the "
                         "search_path unless VERBOSE): makes all generated SQL schema-qualified")
    ap.add_argument("--query", metavar="FILE", help="the SQL statement the plan came from (copied into --script DIR)")
    ap.add_argument("--compare", nargs="+", metavar="PATH",
                    help="compare plans: a --script DIR, or BASELINE.json CANDIDATE.json [...]")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)

    if args.compare:
        from .compare import compare_dir, compare_files
        try:
            cfg = Config.from_overrides(args.set)
            paths = [Path(p) for p in args.compare]
            text, rows = compare_dir(paths[0], cfg) if len(paths) == 1 and paths[0].is_dir() \
                else compare_files(paths, cfg)
        except (ValueError, OSError, KeyError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps([{k: v for k, v in r.items() if k != "deltas"} for r in rows], indent=2, default=str))
        else:
            print(text)
        return 0

    try:
        cfg = Config.from_overrides(args.set)
        text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8", errors="replace")
        results = analyze_text(text, cfg, args.sanitize, args.salt, args.schema)
    except (ParseError, ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    from . import report_html, report_text
    color = args.color == "always" or (args.color == "auto" and sys.stdout.isatty())
    minsev = SEVERITY_VALUES[args.min_severity]
    worst = 0
    docs = []
    for i, (plan, findings) in enumerate(results):
        worst = max([worst] + [f.severity for f in findings])
        if args.html:
            path = Path(args.html)
            if len(results) > 1:
                path = path.with_name(f"{path.stem}-{i + 1}{path.suffix}")
            path.write_text(report_html.render(plan, findings), encoding="utf-8")
            print(f"wrote {path}", file=sys.stderr)
        if args.json:
            docs.append(_json_doc(plan, findings))
        else:
            if len(results) > 1:
                print(f"######## Plan {i + 1} of {len(results)} ########")
            print(report_text.render(plan, findings, color, args.top, minsev, not args.no_actions))
        if args.script:
            from .experiments import generate
            d = Path(args.script) if len(results) == 1 else Path(args.script) / f"plan{i + 1}"
            try:
                rels = sorted({n.get("Relation Name") for n in plan.nodes if n.get("Relation Name") and not n.get("Schema")})
                qual = sorted({(n.get("Schema"), n.get("Relation Name")) for n in plan.nodes
                               if n.get("Relation Name") and n.get("Schema")})
                man = generate(plan.m.get("actions", []), d, args.query, args.file, plan.settings, rels, args.schema,
                               qual, fingerprint(plan))
            except (ValueError, OSError) as e:
                print(f"error: {e}", file=sys.stderr)
                return 1
            for note in man.get("notes", []):
                print(f"note: {note}", file=sys.stderr)
            print(f"\nwrote {d}/experiments.sql ({len(man['experiments'])} experiments). Next:\n"
                  f"  cd {d} && psql -X -d <database> -f experiments.sql\n"
                  f"  python -m explain_analyzer --compare {d}", file=sys.stderr)
    if args.json:
        print(json.dumps(docs if len(docs) > 1 else docs[0], indent=2))
    if args.fail_on and worst >= SEVERITY_VALUES[args.fail_on]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
