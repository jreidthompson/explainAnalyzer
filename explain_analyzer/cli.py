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
from .rules import run_rules
from .sanitize import Sanitizer


def analyze_text(text: str, cfg: Config | None = None, sanitize: str | None = None,
                 salt: str = "") -> list[tuple[Plan, list[Finding]]]:
    """Parse + analyse. ``sanitize`` is None, 'literals' or 'names'."""
    cfg = cfg or Config()
    out = []
    for plan in parse_input(text):
        compute(plan, cfg)
        if sanitize:
            Sanitizer(names=sanitize == "names", salt=salt).plan(plan)
        out.append((plan, run_rules(plan, cfg)))
    return out


def _json_doc(plan: Plan, findings: list[Finding]) -> dict:
    return {
        "planning_time_ms": plan.planning_time,
        "execution_time_ms": plan.execution_time,
        "analyzed": plan.m["analyzed"],
        "findings": [{"rule": f.rule, "severity": f.severity_name, "title": f.title,
                      "detail": f.detail, "suggestion": f.suggestion, "node": f.node_id,
                      "context": f.context,
                      "impact_ms": round(f.impact_ms, 3)} for f in findings],
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
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)

    try:
        cfg = Config.from_overrides(args.set)
        text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8", errors="replace")
        results = analyze_text(text, cfg, args.sanitize, args.salt)
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
            print(report_text.render(plan, findings, color, args.top, minsev))
    if args.json:
        print(json.dumps(docs if len(docs) > 1 else docs[0], indent=2))
    if args.fail_on and worst >= SEVERITY_VALUES[args.fail_on]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
