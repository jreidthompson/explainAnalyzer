"""Compare captured plans: did an experiment help, and what changed?"""
from __future__ import annotations

import json
from pathlib import Path

from .context import join_on
from .metrics import Config
from .model import Plan

FASTER, SLOWER = 0.90, 1.10     # outside this band vs baseline counts as a real change


def _load(path: Path, cfg: Config):
    from .cli import analyze_text
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
        if not txt.strip():
            return None, "empty output (the experiment failed - see psql's error message)"
        (plan, findings), *_ = analyze_text(txt, cfg)
        return (plan, findings), None
    except Exception as e:  # noqa: BLE001 - report, don't crash the comparison
        return None, f"could not parse: {e}"


def _sig(n) -> str:
    return f"{n.label()}|{join_on(n, 200)}"


def summarize(plan: Plan, findings) -> dict:
    return {
        "exec_ms": plan.execution_time, "plan_ms": plan.planning_time,
        "critical": sum(f.severity == 3 for f in findings), "warning": sum(f.severity == 2 for f in findings),
        "rules": {f.rule: sum(1 for g in findings if g.rule == f.rule) for f in findings},
        "findings": {(f.rule, (next((n for n in plan.nodes if n.id == f.node_id), None) and
                               _sig(next(n for n in plan.nodes if n.id == f.node_id))) or "") for f in findings},
    }


def node_deltas(base: Plan, cand: Plan, top: int = 4) -> list[str]:
    """Pair nodes by signature (in order of appearance) and report the biggest exclusive-time changes."""
    seen: dict[str, list] = {}
    for n in base.nodes:
        seen.setdefault(_sig(n), []).append(n)
    used: dict[str, int] = {}
    rows = []
    for n in cand.nodes:
        s = _sig(n)
        i = used.get(s, 0)
        used[s] = i + 1
        b = seen.get(s, [])
        if i < len(b):
            rows.append((n.m["excl_ms"] - b[i].m["excl_ms"], f"{n.label()}: {b[i].m['excl_ms']:,.1f} -> {n.m['excl_ms']:,.1f} ms"))
        else:
            rows.append((n.m["excl_ms"], f"NEW {n.label()}: {n.m['excl_ms']:,.1f} ms"))
    gone = [n for n in base.nodes if used.get(_sig(n), 0) == 0]
    rows.extend((-n.m["excl_ms"], f"GONE {n.label()}: was {n.m['excl_ms']:,.1f} ms") for n in gone)
    rows.sort(key=lambda r: -abs(r[0]))
    return [r[1] for r in rows[:top] if abs(r[0]) >= 0.5]


def compare(base_path: Path, cands: list[tuple[str, Path, dict]], cfg: Config) -> tuple[str, list[dict]]:
    """cands: (label, path, manifest entry). Returns (text, rows)."""
    b, err = _load(base_path, cfg)
    if b is None:
        return f"baseline {base_path}: {err}", []
    bplan, bfind = b
    bs = summarize(bplan, bfind)
    base_ms = bs["exec_ms"] or bplan.root.m.get("incl_ms") or 0.0
    rows: list[dict] = [{"label": "baseline", "exec_ms": base_ms, "critical": bs["critical"], "warning": bs["warning"],
                         "verdict": "-", "speedup": 1.0}]
    details: list[str] = []
    for label, path, meta in cands:
        c, err = _load(path, cfg)
        if c is None:
            rows.append({"label": label, "exec_ms": None, "verdict": f"FAILED: {err}", "meta": meta})
            continue
        cplan, cfind = c
        cs = summarize(cplan, cfind)
        ms = cs["exec_ms"] or cplan.root.m.get("incl_ms") or 0.0
        ratio = ms / base_ms if base_ms else 1.0
        new = cs["findings"] - bs["findings"]
        resolved = bs["findings"] - cs["findings"]
        if ratio <= FASTER:
            verdict = "FASTER"
        elif ratio >= SLOWER:
            verdict = "SLOWER"
        else:
            verdict = "no change"
        est_fixed = bs["rules"].get("row-misestimate", 0) - cs["rules"].get("row-misestimate", 0)
        if verdict == "no change" and est_fixed > 0:
            verdict = f"no speed change, but {est_fixed} estimate error(s) fixed"
        if meta and meta.get("diagnostic_only") and verdict == "FASTER":
            verdict = "FASTER (diagnostic only - fix the cause, do not deploy)"
        rows.append({"label": label, "exec_ms": ms, "critical": cs["critical"], "warning": cs["warning"],
                     "verdict": verdict, "speedup": (base_ms / ms) if ms else None, "meta": meta,
                     "resolved": len(resolved), "new": len(new),
                     "deltas": node_deltas(bplan, cplan)})
    w = max(len(r["label"]) for r in rows)
    lines = [f"{'Experiment':<{w}}  {'exec ms':>12}  {'vs baseline':>12}  crit/warn  verdict"]
    for r in rows:
        if r["exec_ms"] is None:
            lines.append(f"{r['label']:<{w}}  {'-':>12}  {'-':>12}  {'-':>9}  {r['verdict']}")
            continue
        sp = r.get("speedup")
        if r["label"] == "baseline":
            rel = "-"
        elif r["verdict"] == "no change" or r["verdict"].startswith("no speed change"):
            rel = "~same"
        else:
            rel = f"{sp:,.1f}x faster" if sp and sp >= 1 else f"{(1 / sp if sp else 0):,.1f}x slower"
        lines.append(f"{r['label']:<{w}}  {r['exec_ms']:>12,.1f}  {rel:>12}  {r['critical']:>4}/{r['warning']:<4}  {r['verdict']}")
    good = [r for r in rows if str(r["verdict"]).startswith("FASTER") and "diagnostic" not in r["verdict"]]
    if good:
        lines += ["", "What changed in the winning experiments:"]
        for r in sorted(good, key=lambda r: r["exec_ms"]):
            lines.append(f"  {r['label']}")
            lines += [f"      {d}" for d in r.get("deltas", [])]
            if r.get("resolved") or r.get("new"):
                lines.append(f"      findings resolved: {r.get('resolved', 0)}, new: {r.get('new', 0)}")
        fastest = min(good, key=lambda r: r["exec_ms"])
        saved_best = base_ms - fastest["exec_ms"]
        # prefer the smallest change that captures nearly all of the gain
        enough = [r for r in good if base_ms - r["exec_ms"] >= 0.95 * saved_best]
        best = min(enough, key=lambda r: (len((r.get("meta") or {}).get("apply") or []), r["exec_ms"]))
        apply = (best.get("meta") or {}).get("apply") or []
        if apply:
            note = "" if best is fastest else (f" (captures {100 * (base_ms - best['exec_ms']) / saved_best:.0f}% of the best "
                                               f"result with fewer changes than '{fastest['label']}')")
            lines += ["", f"To make '{best['label']}' permanent{note}:"] + [f"  {s}" for s in apply]
            lines += ["", "Then re-capture the plan and run the analyzer again: the next bottleneck is now visible."]
    helpful = [r for r in rows if str(r["verdict"]).startswith("no speed change") and not (r.get("meta") or {}).get("diagnostic_only")]
    if helpful:
        lines += ["", "Improved the planner's estimates without changing this run's speed (worth keeping if the query "
                      "runs with other parameter values or on larger data - bad estimates cause plan flips):"]
        for r in helpful:
            lines.append(f"  {r['label']}")
            lines += [f"      {s}" for s in (r.get("meta") or {}).get("apply") or []]
    if not good:
        lines += ["", "No experiment beat the baseline by more than 10%. Re-capture after fixing statistics (stage 1), "
                      "and check the findings that remain."]
    lines += ["", "Note: each number is one execution; differences under ~10% are noise. Re-run to confirm a win."]
    return "\n".join(lines), rows


def compare_dir(d: Path, cfg: Config) -> tuple[str, list[dict]]:
    man = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    cands = []
    for e in man["experiments"]:
        cands.append((f"{e['id']} {e['title']} [{e['variant']}]"[:96], d / e["file"], e))
    text, rows = compare(d / man["baseline"], cands, cfg)
    skipped = man.get("skipped") or []
    if skipped:
        text += "\n\nNot testable automatically (do these by hand):\n" + "\n".join(
            f"  {s['id']} {s['title']} ({s['reason']})" for s in skipped)
    return text, rows


def compare_files(paths: list[Path], cfg: Config) -> tuple[str, list[dict]]:
    return compare(paths[0], [(p.name, p, {}) for p in paths[1:]], cfg)
