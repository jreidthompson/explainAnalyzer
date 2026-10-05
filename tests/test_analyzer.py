import ast
import json
import re
import unittest
from pathlib import Path

from explain_analyzer import report_html, report_text
from explain_analyzer.cli import analyze_text, main
from explain_analyzer.metrics import Config

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).parent / "fixtures"


def N(node_type, rows=1, act=None, loops=1, total=None, children=(), **kw):
    d = {"Node Type": node_type, "Startup Cost": 0.0, "Total Cost": 10.0, "Plan Rows": rows,
         "Plan Width": 8}
    if act is not None:
        d.update({"Actual Startup Time": 0.0, "Actual Total Time": total if total is not None else 1.0,
                  "Actual Rows": act, "Actual Loops": loops})
    d.update(kw)
    if children:
        d["Plans"] = list(children)
    return d


def run(plan_dict, **extra):
    doc = [{"Plan": plan_dict, **extra}]
    (plan, findings), = analyze_text(json.dumps(doc))
    return plan, findings


def rules_of(findings):
    return {f.rule for f in findings}


class ParserTests(unittest.TestCase):
    def test_real_text_and_json_agree(self):
        for name in ("real_nl", "real_cte"):
            (pj, _), = analyze_text((FIX / f"{name}.json").read_text())
            (pt, _), = analyze_text((FIX / f"{name}.txt").read_text())
            self.assertEqual([n.label() for n in pj.nodes], [n.label() for n in pt.nodes], name)
            for a, b in zip(pj.nodes, pt.nodes):
                self.assertEqual(a.m["loops"], b.m["loops"])
                self.assertEqual(a.m["act_rows"], b.m["act_rows"])
                self.assertEqual(a.m["est_rows"], b.m["est_rows"])
                self.assertEqual(a.relationship, b.relationship, a.label())
                self.assertEqual(a.get("Filter"), b.get("Filter"))
            # buffer counts differ between the two captured runs, but kinds must match
            self.assertTrue(pt.root.m["buf_incl"])
            self.assertTrue(set(pt.root.m["buf_incl"]) <= set(pj.root.m["buf_incl"]))

    def test_cte_time_not_double_counted(self):
        (p, _), = analyze_text((FIX / "real_cte.txt").read_text())
        total = p.execution_time
        self.assertLess(sum(n.m["excl_ms"] for n in p.nodes), total * 1.15)

    def test_pg18_text_format(self):
        txt = """\
Index Only Scan using i on t  (cost=0.42..8.44 rows=1 width=4) (actual time=0.020..0.021 rows=1.00 loops=3)
  Index Cond: (id = 5)
  Heap Fetches: 7
  Index Searches: 3
  Buffers: shared hit=4 read=1
Planning Time: 0.1 ms
Execution Time: 0.2 ms
"""
        (p, _), = analyze_text(txt)
        n = p.root
        self.assertEqual(n.get("Actual Rows"), 1.0)
        self.assertEqual(n.get("Index Searches"), 3)
        self.assertEqual(n.get("Heap Fetches"), 7)
        self.assertEqual(n.get("Shared Read Blocks"), 1)

    def test_psql_aligned_output(self):
        txt = """\
                          QUERY PLAN
--------------------------------------------------------------
 Seq Scan on t  (cost=0.00..10.00 rows=5 width=4)
   Filter: (a > 1)
(2 rows)
"""
        (p, _), = analyze_text(txt)
        self.assertEqual(p.root.get("Filter"), "(a > 1)")
        self.assertFalse(p.m["analyzed"])

    def test_never_executed_and_sort_text(self):
        txt = """\
Nested Loop  (cost=0.00..10.00 rows=1 width=4) (actual time=0.1..0.1 rows=0 loops=1)
  ->  Seq Scan on a  (cost=0.00..1.00 rows=1 width=4) (actual time=0.1..0.1 rows=0 loops=1)
  ->  Sort  (cost=0.00..1.00 rows=1 width=4) (never executed)
        Sort Key: x
"""
        (p, _), = analyze_text(txt)
        self.assertTrue(p.nodes[2].m["never"])

    def test_two_plans_in_one_input(self):
        one = "Seq Scan on t  (cost=0.00..1.00 rows=1 width=4) (actual time=0.1..0.1 rows=1 loops=1)\nExecution Time: 1 ms\n"
        res = analyze_text(one + one)
        self.assertEqual(len(res), 2)

    def test_json_wrapped_in_psql_noise(self):
        doc = json.dumps([{"Plan": N("Result", act=1)}], indent=2)
        wrapped = " QUERY PLAN\n-----\n" + "\n".join(" " + l + " +" for l in doc.splitlines()) + "\n(1 row)\n"
        self.assertEqual(len(analyze_text(wrapped)), 1)


class MetricTests(unittest.TestCase):
    def test_exclusive_time_uses_loops(self):
        inner = N("Index Scan", act=1, loops=100, total=0.5, **{"Relation Name": "d", "Parent Relationship": "Inner"})
        outer = N("Seq Scan", act=100, total=2.0, **{"Parent Relationship": "Outer", "Relation Name": "t"})
        p, _ = run(N("Nested Loop", act=100, total=60.0, children=[outer, inner]), **{"Execution Time": 60.0})
        nl, o, i = p.nodes
        self.assertAlmostEqual(i.m["incl_ms"], 50.0)
        self.assertAlmostEqual(nl.m["excl_ms"], 8.0)

    def test_parallel_time_divided_by_participants(self):
        scan = N("Seq Scan", act=10, loops=3, total=30.0, **{"Parallel Aware": True, "Relation Name": "t"})
        g = N("Gather", act=30, total=40.0, children=[scan], **{"Workers Planned": 2, "Workers Launched": 2})
        p, _ = run(g, **{"Execution Time": 40.0})
        self.assertAlmostEqual(p.nodes[1].m["incl_ms"], 30.0)
        self.assertAlmostEqual(p.nodes[0].m["excl_ms"], 10.0)

    def test_over_estimate_under_limit_ignored(self):
        scan = N("Seq Scan", rows=100000, act=10, **{"Relation Name": "t"})
        p, f = run(N("Limit", rows=10, act=10, children=[scan]))
        self.assertNotIn("row-misestimate", rules_of(f))


class RuleTests(unittest.TestCase):
    def test_misestimate_origin_and_nested_loop(self):
        outer = N("Seq Scan", rows=10, act=50000, total=40.0, **{"Relation Name": "t", "Parent Relationship": "Outer"})
        inner = N("Index Scan", rows=1, act=1, loops=50000, total=0.01,
                  **{"Relation Name": "d", "Index Name": "i", "Parent Relationship": "Inner"})
        p, f = run(N("Nested Loop", rows=10, act=50000, total=600.0, children=[outer, inner]),
                   **{"Execution Time": 600.0})
        mis = [x for x in f if x.rule == "row-misestimate"]
        crit = [x for x in mis if x.severity == 3]
        self.assertEqual([x.node_id for x in crit], [2])  # only the origin is critical
        self.assertEqual([x.node_id for x in mis if x.severity == 1], [1])  # parent inherits
        self.assertEqual([x.severity for x in f if x.rule == "nested-loop"], [3])

    def test_sort_and_hash_spill(self):
        s = N("Sort", act=5, total=5.0, **{"Sort Method": "external merge", "Sort Space Type": "Disk", "Sort Space Used": 4096})
        h = N("Hash", act=5, total=5.0, **{"Hash Batches": 8, "Peak Memory Usage": 4000})
        p, f = run(N("Hash Join", act=5, total=20, children=[s, h]))
        self.assertTrue({"sort-spill", "hash-spill"} <= rules_of(f))

    def test_heap_fetches(self):
        n = N("Index Only Scan", rows=5000, act=5000, **{"Heap Fetches": 5000, "Relation Name": "t"})
        _, f = run(n)
        self.assertIn("heap-fetches", rules_of(f))

    def test_seq_scan_filter_and_hot(self):
        n = N("Seq Scan", rows=10, act=10, total=900.0, **{"Relation Name": "t", "Filter": "(a = 1)", "Rows Removed by Filter": 5_000_000})
        _, f = run(n, **{"Execution Time": 900.0})
        self.assertTrue({"seq-scan-filter", "hot-node"} <= rules_of(f))

    def test_workers_jit_trigger_planning(self):
        g = N("Gather", act=1, total=100.0, **{"Workers Planned": 4, "Workers Launched": 1})
        _, f = run(g, **{"Execution Time": 100.0, "Planning Time": 80.0,
                         "JIT": {"Functions": 5, "Timing": {"Total": 40.0}},
                         "Triggers": [{"Trigger Name": "fk", "Time": 20.0, "Calls": 3}]})
        self.assertTrue({"workers-launched", "jit", "planning-time", "trigger"} <= rules_of(f))

    def test_disk_reads(self):
        n = N("Seq Scan", act=1, total=50.0, **{"Relation Name": "t", "Shared Read Blocks": 5000, "Shared Hit Blocks": 10})
        _, f = run(n, **{"Execution Time": 50.0})
        self.assertIn("disk-reads", rules_of(f))

    def test_no_analyze(self):
        p, f = run(N("Seq Scan", rows=1000000, **{"Relation Name": "t", "Filter": "(a = 1)"}))
        self.assertIn("no-analyze", rules_of(f))
        self.assertNotIn("hot-node", rules_of(f))

    def test_threshold_override(self):
        cfg = Config.from_overrides(["mis_warn=1000"])
        self.assertEqual(cfg.mis_warn, 1000.0)
        with self.assertRaises(ValueError):
            Config.from_overrides(["nope=1"])


class OutputTests(unittest.TestCase):
    def _sample(self):
        n = N("Seq Scan", act=10, total=5.0, **{"Relation Name": "secret_tbl", "Filter": "(email = 'bob@corp.com'::text)",
                                                  "Rows Removed by Filter": 99999999})
        return run(n, **{"Execution Time": 5.0})

    def test_html_self_contained_and_escaped(self):
        n = N("Seq Scan", act=1, **{"Relation Name": "t", "Filter": "(x = '</script><img src=http://evil/>')"})
        p, f = run(n)
        html = report_html.render(p, f)
        markup = re.sub(r'<script id="data".*?</script>', "", html, flags=re.S)
        self.assertNotRegex(markup, r"(src|href)\s*=\s*[\"']?https?:")
        self.assertEqual(html.count("</script>"), 2)  # only our two script elements
        self.assertNotIn("<img", html)
        self.assertNotIn("innerHTML", html)

    def test_text_report_renders(self):
        p, f = self._sample()
        out = report_text.render(p, f)
        self.assertIn("Seq Scan on secret_tbl", out)

    def test_sanitize_literals_and_names(self):
        txt = json.dumps([{"Plan": N("Seq Scan", act=10, **{"Relation Name": "secret_tbl", "Alias": "s",
                                     "Filter": "((email)::text = 'bob@corp.com'::text AND n > 42)"})}])
        (p, f), = analyze_text(txt, sanitize="literals")
        self.assertNotIn("bob", p.root.get("Filter"))
        self.assertNotIn("42", p.root.get("Filter"))
        self.assertIn("email", p.root.get("Filter"))
        (p, f), = analyze_text(txt, sanitize="names", salt="x")
        blob = report_text.render(p, f) + report_html.render(p, f)
        for secret in ("secret_tbl", "email", "bob", "corp.com"):
            self.assertNotIn(secret, blob)
        self.assertIn("::text", p.root.get("Filter"))

    def test_cli_fail_on(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main([str(FIX / "real_nl.json"), "--fail-on", "critical", "--color", "never"])
            rc_ok = main([str(FIX / "real_nl.json"), "--json", "--set", "mis_warn=100000", "--set",
                          "hot_crit_pct=101", "--set", "filter_min_removed=10000000"])
        self.assertEqual(rc, 2)
        self.assertEqual(rc_ok, 0)


class OfflineGuarantee(unittest.TestCase):
    def test_no_network_imports(self):
        banned = {"socket", "urllib", "http", "requests", "ssl", "ftplib", "smtplib", "asyncio",
                  "subprocess", "webbrowser", "xmlrpc", "telnetlib"}
        for path in (ROOT / "explain_analyzer").glob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    mods = [node.module]
                for m in mods:
                    self.assertNotIn(m.split(".")[0], banned, f"{path.name} imports {m}")

    def test_html_template_has_no_external_urls(self):
        p, f = analyze_text((FIX / "real_nl.json").read_text())[0]
        self.assertNotRegex(report_html.render(p, f), r"https?://")


if __name__ == "__main__":
    unittest.main()
