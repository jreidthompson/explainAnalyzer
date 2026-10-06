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

    def test_psql_noise_before_and_after_plan(self):
        body = (FIX / "real_nl.txt").read_text()
        for pre, post in (("BEGIN\nSET\n", "ROLLBACK\n"), ("Timing is on.\n", "Time: 3.2 ms\n")):
            (p, _), = analyze_text(pre + body + post)
            self.assertTrue(p.m["analyzed"])
            self.assertEqual(p.root.node_type, "Nested Loop")
            self.assertEqual(len(p.nodes), 3)

    def test_costs_off_plan_still_parses(self):
        txt = "Nested Loop\n  ->  Seq Scan on a\n  ->  Index Scan using i on b\n        Index Cond: (id = a.id)\n"
        (p, _), = analyze_text(txt)
        self.assertEqual([n.node_type for n in p.nodes], ["Nested Loop", "Seq Scan", "Index Scan"])

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


class ContextTests(unittest.TestCase):
    def _plan(self):
        outer = N("Seq Scan", rows=10, act=3_600_000, total=900.0,
                  **{"Relation Name": "orders", "Alias": "o", "Parent Relationship": "Outer"})
        scan = N("Index Scan", rows=1, act=1, total=0.01, loops=3_600_000,
                 **{"Relation Name": "customers", "Alias": "c", "Index Name": "customers_pkey",
                    "Index Cond": "(c.id = o.customer_id)", "Parent Relationship": "Outer"})
        memo = N("Memoize", rows=1, act=1, loops=3_600_000, total=0.05, children=[scan],
                 **{"Cache Key": "o.customer_id", "Parent Relationship": "Inner"})
        nl = N("Nested Loop", rows=10, act=3_600_000, total=300000.0, children=[outer, memo],
               **{"Join Type": "Left"})
        return run(N("Limit", rows=10, act=10, children=[nl]), **{"Execution Time": 1_000_000.0})

    def test_nested_loop_finding_names_tables_and_condition(self):
        p, f = self._plan()
        nl = next(x for x in f if x.rule == "nested-loop")
        ctx = "\n".join(nl.context)
        self.assertIn("orders o", ctx)
        self.assertIn("customers c", ctx)
        self.assertIn("(c.id = o.customer_id)", ctx)
        self.assertIn("Location: #1 Limit > #2 Nested Loop Left Join", ctx)
        self.assertIn("Memoize > Index Scan using customers_pkey on customers c", nl.detail)

    def test_context_in_reports(self):
        p, f = self._plan()
        self.assertIn("| On: (c.id = o.customer_id)", report_text.render(p, f))
        self.assertIn("(c.id = o.customer_id)", report_html.render(p, f))
        self.assertIn("(c.id = o.customer_id)", json.dumps(report_html.build_data(p, f)))


class HashBatchTests(unittest.TestCase):
    SETTINGS = {"Settings": {"work_mem": "6GB", "hash_mem_multiplier": "4"}}

    def _hash_plan(self, **hash_props):
        scan = N("Seq Scan", rows=5_000_000_000, act=40_000, total=5.0, **{"Relation Name": "big"})
        h = N("Hash", rows=5_000_000_000, act=40_000, total=6.0, children=[scan],
              **{"Plan Width": 100, "Peak Memory Usage": 95232, **hash_props})
        probe = N("Seq Scan", rows=10, act=10, **{"Relation Name": "p"})
        return run(N("Hash Join", rows=10, act=10, total=20.0, children=[probe, h]), **self.SETTINGS)

    def test_planned_batches_blamed_on_estimate_not_work_mem(self):
        p, f = self._hash_plan(**{"Hash Batches": 64, "Original Hash Batches": 64})
        h = next(x for x in f if x.rule == "hash-spill")
        self.assertNotIn("exceeded work_mem", h.detail)
        self.assertIn("before execution started", h.title)
        self.assertIn("24.0 GB", h.detail)          # 6GB x 4
        self.assertIn("over-estimated", h.suggestion)

    def test_runtime_growth_reported_as_memory_pressure(self):
        p, f = self._hash_plan(**{"Hash Batches": 64, "Original Hash Batches": 1})
        h = next(x for x in f if x.rule == "hash-spill")
        self.assertIn("grew from 1 to 64", h.title)

    def test_text_originally_notation(self):
        txt = """\
Hash  (cost=1.00..2.00 rows=100 width=4) (actual time=1.0..2.0 rows=100 loops=1)
  Buckets: 1024 (originally 1024)  Batches: 64 (originally 8)  Memory Usage: 93000kB
"""
        (p, _), = analyze_text(txt)
        self.assertEqual((p.root.get("Hash Batches"), p.root.get("Original Hash Batches")), (64, 8))


class OriginTests(unittest.TestCase):
    def test_inherited_error_names_the_source_node(self):
        scan = N("Seq Scan", rows=1_000_000, act=1000, total=5.0,
                 **{"Relation Name": "events", "Alias": "e", "Filter": "(kind = 'x'::text)"})
        join = N("Hash Join", rows=900_000, act=900, total=6.0, children=[scan],
                 **{"Hash Cond": "(e.id = u.id)"})
        top = N("Sort", rows=900_000, act=900, total=7.0, children=[join])
        p, f = run(top)
        inh = [x for x in f if x.rule == "row-misestimate" and x.node_id == 1]
        self.assertEqual(len(inh), 1)
        self.assertIn("inherited from #3 Seq Scan on events e", inh[0].detail)
        self.assertIn("kind = 'x'", inh[0].detail)

    def test_hash_finding_points_at_origin(self):
        scan = N("Seq Scan", rows=5_000_000_000, act=40_000, total=5.0, **{"Relation Name": "big"})
        h = N("Hash", rows=5_000_000_000, act=40_000, total=6.0, children=[scan],
              **{"Plan Width": 100, "Peak Memory Usage": 95232, "Hash Batches": 64, "Original Hash Batches": 64})
        p, f = run(N("Hash Join", rows=10, act=10, total=20.0, children=[N("Seq Scan", rows=10, act=10, **{"Relation Name": "p"}), h]),
                   Settings={"work_mem": "4GB"})
        hs = next(x for x in f if x.rule == "hash-spill")
        self.assertIn("error starts at #4 Seq Scan on big", hs.suggestion)
        self.assertIn("server default", hs.detail)


class JoinKeyDisplayTests(unittest.TestCase):
    def _plan(self):
        o = N("Seq Scan", rows=10, act=10, **{"Relation Name": "orders", "Alias": "o", "Parent Relationship": "Outer"})
        c = N("Index Scan", rows=1, act=1, loops=10, **{"Relation Name": "customers", "Alias": "c",
              "Index Name": "customers_pkey", "Index Cond": "(c.id = o.customer_id)", "Parent Relationship": "Inner"})
        nl = N("Nested Loop", rows=10, act=10, total=5000.0, children=[o, c])
        hj = N("Hash Join", rows=10, act=10, total=6000.0, **{"Hash Cond": "(o.id = i.order_id)"},
               children=[nl, N("Hash", rows=5, act=5, total=1.0, **{"Parent Relationship": "Inner"},
                               children=[N("Seq Scan", rows=5, act=5, **{"Relation Name": "items", "Alias": "i"})])])
        return run(hj, **{"Execution Time": 6000.0})

    def test_text_tree_shows_join_keys(self):
        p, f = self._plan()
        out = report_text.render(p, f)
        self.assertIn("on: (o.id = i.order_id)", out)
        self.assertIn("on: (c.id = o.customer_id)", out)
        self.assertIn("tables: orders o, customers c  <->  items i", out)

    def test_findings_headline_includes_join_key(self):
        p, f = self._plan()
        hot = next(x for x in f if x.rule == "hot-node" and x.node_id == 1)
        self.assertIn("Hash Join on (o.id = i.order_id)", hot.detail)

    def test_html_tree_and_table_have_join_columns(self):
        p, f = self._plan()
        data = report_html.build_data(p, f)
        self.assertEqual(data["nodes"][0]["on"], "(o.id = i.order_id)")
        html = report_html.render(p, f)
        self.assertIn("Join / lookup on", html)


class QualifyTests(unittest.TestCase):
    def test_bare_index_cond_columns_get_the_scan_alias(self):
        from explain_analyzer.context import qualify
        from explain_analyzer.model import Node
        n = Node({"Node Type": "Index Scan", "Alias": "d", "Relation Name": "d"})
        self.assertEqual(qualify(n, "(id = t.k)"), "(d.id = t.k)")
        self.assertEqual(qualify(n, "((a = 1) AND (b >= t.x))"), "((d.a = 1) AND (d.b >= t.x))")
        self.assertEqual(qualify(n, "(d.id = t.k)"), "(d.id = t.k)")  # already qualified

    def test_join_line_uses_qualified_inner_cond(self):
        (p, f), = analyze_text((FIX / "real_nl.json").read_text())
        self.assertIn("on: (d.id = t.k)", report_text.render(p, f))


class KeysDisplayTests(unittest.TestCase):
    def _plan(self):
        scan = N("Seq Scan", rows=10, act=10, **{"Relation Name": "t", "Alias": "t", "Filter": "(x > 5)"})
        srt = N("Sort", rows=10, act=10, total=3.0, children=[scan],
                **{"Sort Key": ["t.a", "t.b DESC"], "Sort Method": "external merge",
                   "Sort Space Type": "Disk", "Sort Space Used": 2048})
        agg = N("Aggregate", rows=5, act=5, total=4.0, children=[srt], **{"Strategy": "Sorted", "Group Key": ["t.a"]})
        return run(agg)

    def test_text_tree_shows_sort_group_filter_keys(self):
        p, f = self._plan()
        out = report_text.render(p, f)
        for expected in ("group: t.a", "sort: t.a, t.b DESC", "filter: (x > 5)",
                         "sort ran: external merge, Disk 2.0MB"):
            self.assertIn(expected, out)

    def test_html_has_keys_and_wide_table(self):
        p, f = self._plan()
        data = report_html.build_data(p, f)
        self.assertIn(["sort", "t.a, t.b DESC"], data["nodes"][1]["keys"])
        html = report_html.render(p, f)
        self.assertIn("'Keys'", html)
        self.assertNotIn("max-width:1300px", html)
        self.assertNotIn("row.nextSibling", html)


def actions_for(plan_dict, **extra):
    doc = [{"Plan": plan_dict, **extra}]
    (plan, findings), = analyze_text(json.dumps(doc))
    return plan, findings, plan.m["actions"]


def sql_of(actions):
    out = []
    for a in actions:
        for _, stmts in a.try_variants:
            out += stmts
        out += a.apply
    return "\n".join(out)


class RemedyTests(unittest.TestCase):
    def test_correlated_subplan_gets_index_and_rewrite(self):
        scan = N("Seq Scan", rows=3, act=0, loops=2484, total=7.6,
                 **{"Relation Name": "items", "Alias": "i", "Filter": "(order_id = o.id)",
                    "Rows Removed by Filter": 200000, "Parent Relationship": "SubPlan", "Subplan Name": "SubPlan 1"})
        agg = N("Aggregate", act=1, loops=2484, total=7.7, children=[scan],
                **{"Parent Relationship": "SubPlan", "Subplan Name": "SubPlan 1"})
        outer = N("Seq Scan", rows=2484, act=2484, total=20.0, **{"Relation Name": "orders", "Alias": "o"})
        top = N("Hash Join", rows=2484, act=2484, total=20000.0, children=[outer, agg])
        p, f, acts = actions_for(top, **{"Execution Time": 20000.0})
        sql = sql_of(acts)
        self.assertIn("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_items_order_id ON items (order_id);", sql)
        self.assertIn("CREATE INDEX idx_items_order_id ON items (order_id);", sql)   # rollback-safe variant
        self.assertTrue(any(a.kind == "rewrite" and a.manual for a in acts))
        self.assertTrue(all(a.stage == 2 for a in acts if a.kind in ("index", "rewrite")))
        self.assertTrue(any(x.actions for x in f if x.rule == "seq-scan-filter"))

    def test_equality_columns_before_range_column(self):
        n = N("Seq Scan", rows=10, act=10, total=900.0,
              **{"Relation Name": "orders", "Alias": "o", "Rows Removed by Filter": 5_000_000,
                 "Filter": "((dow >= 10) AND (dow <= 200) AND (status = 3))"})
        _, _, acts = actions_for(n, **{"Execution Time": 900.0})
        self.assertIn("ON orders (status, dow)", sql_of(acts))

    def test_expression_and_trigram_indexes(self):
        n = N("Seq Scan", rows=10, act=10, total=900.0,
              **{"Relation Name": "users", "Rows Removed by Filter": 5_000_000,
                 "Filter": "((lower((email)::text) = 'a'::text) AND (name ~~ '%bob%'::text))"})
        _, _, acts = actions_for(n, **{"Execution Time": 900.0})
        sql = sql_of(acts)
        self.assertIn("(lower(email))", sql)
        self.assertIn("USING gin (name gin_trgm_ops)", sql)
        self.assertIn("CREATE EXTENSION IF NOT EXISTS pg_trgm;", sql)

    def test_no_index_suggested_when_filter_keeps_half_the_rows(self):
        n = N("Seq Scan", rows=500000, act=500000, total=900.0,
              **{"Relation Name": "o", "Filter": "(amt < 50)", "Rows Removed by Filter": 500000})
        _, _, acts = actions_for(n, **{"Execution Time": 900.0})
        self.assertFalse([a for a in acts if a.kind == "index"])

    def test_correlated_columns_get_extended_statistics_and_analyze_first(self):
        scan = N("Seq Scan", rows=17, act=2000, total=8.0, **{"Relation Name": "t", "Rows Removed by Filter": 198000,
                 "Filter": "((a = 1) AND (b = 1))", "Parent Relationship": "Outer"})
        top = N("Nested Loop", rows=17, act=2000, total=10.0, children=[scan,
              N("Index Scan", rows=1, act=1, loops=2000, **{"Relation Name": "d", "Index Name": "i",
                "Index Cond": "(id = t.k)", "Parent Relationship": "Inner"})])
        _, _, acts = actions_for(top, **{"Execution Time": 10.0})
        sql = sql_of(acts)
        self.assertIn("CREATE STATISTICS stx_t_a_b ON a, b FROM t;", sql)
        self.assertIn("ANALYZE t;", sql)
        analyze = next(a for a in acts if a.key.startswith("analyze:"))
        self.assertEqual(analyze.stage, 1)
        self.assertEqual(acts[0].id, "A1")
        self.assertLess(acts.index(analyze), acts.index(next(a for a in acts if a.kind == "statistics")))

    def test_sort_spill_variants_and_set_local(self):
        s = N("Sort", act=5, total=50.0, **{"Sort Method": "external merge", "Sort Space Type": "Disk",
                                             "Sort Space Used": 21472, "Sort Key": ["t.a", "t.b DESC"],
                                             "Parent Relationship": "Outer"},
              children=[N("Seq Scan", act=5, **{"Relation Name": "t", "Alias": "t"})])
        _, _, acts = actions_for(s, **{"Execution Time": 50.0})
        wm = next(a for a in acts if a.key.startswith("workmem:sort"))
        self.assertEqual([lbl for lbl, _ in wm.try_variants], ["work_mem 42MB", "work_mem 84MB", "work_mem 168MB"])
        self.assertTrue(all(s_[0].startswith("SET LOCAL work_mem") for _, s_ in wm.try_variants))
        self.assertIn("ON t (t.a, t.b DESC)".replace("t.a", "a").replace("t.b", "b"), sql_of(acts))

    def test_heap_fetches_vacuum_is_not_transactional(self):
        n = N("Index Only Scan", rows=5000, act=5000, **{"Heap Fetches": 5000, "Relation Name": "t"})
        _, _, acts = actions_for(n)
        v = next(a for a in acts if a.kind == "maintenance")
        self.assertFalse(v.transactional)
        self.assertEqual(v.stage, 1)

    def test_nested_loop_diagnostic_is_flagged_not_deployable(self):
        outer = N("Seq Scan", rows=10, act=50000, total=40.0, **{"Relation Name": "t", "Parent Relationship": "Outer"})
        inner = N("Index Scan", rows=1, act=1, loops=50000, total=0.01,
                  **{"Relation Name": "d", "Index Name": "i", "Parent Relationship": "Inner"})
        _, _, acts = actions_for(N("Nested Loop", rows=10, act=50000, total=600.0, children=[outer, inner]),
                                 **{"Execution Time": 600.0})
        d = next(a for a in acts if a.diagnostic_only)
        self.assertEqual(d.apply, [])
        self.assertIn("DIAGNOSTIC ONLY", " ".join(d.caveats))

    def test_planned_hash_batches_point_at_estimate_fix(self):
        scan = N("Seq Scan", rows=5_000_000_000, act=40_000, total=5.0,
                 **{"Relation Name": "big", "Filter": "((a = 1) AND (b = 2))"})
        h = N("Hash", rows=5_000_000_000, act=40_000, total=6.0, children=[scan],
              **{"Plan Width": 100, "Peak Memory Usage": 95232, "Hash Batches": 64, "Original Hash Batches": 64})
        _, _, acts = actions_for(N("Hash Join", rows=10, act=10, total=20.0,
                                   children=[N("Seq Scan", rows=10, act=10, **{"Relation Name": "p"}), h]))
        self.assertTrue(any(a.key.startswith("analyze:") and "big" in a.key for a in acts))
        self.assertFalse(any(a.key.startswith("workmem:hash") for a in acts))


class ExperimentAndCompareTests(unittest.TestCase):
    def _acts(self):
        n = N("Seq Scan", rows=10, act=10, total=900.0,
              **{"Relation Name": "orders", "Rows Removed by Filter": 5_000_000, "Filter": "(status = 3)"})
        v = N("Index Only Scan", rows=5000, act=5000, **{"Heap Fetches": 5000, "Relation Name": "t"})
        p1, _, a1 = actions_for(n, **{"Execution Time": 900.0})
        _, _, a2 = actions_for(v)
        return a1 + a2

    def test_script_runs_each_try_in_a_rolled_back_transaction(self):
        import tempfile
        from explain_analyzer.experiments import generate
        acts = self._acts()
        with tempfile.TemporaryDirectory() as d:
            man = generate(acts, d, source="plan.json")
            sql = (Path(d) / "experiments.sql").read_text()
            self.assertEqual(sql.count("BEGIN;"), sql.count("ROLLBACK;"))
            self.assertIn("out/warmup.json", sql)           # cache warm-up before the measured baseline
            self.assertLess(sql.index("out/warmup.json"), sql.index("out/baseline.json"))
            self.assertIn("CREATE INDEX idx_orders_status ON orders (status);", sql)
            self.assertNotIn("CONCURRENTLY", sql)            # cannot run in a transaction
            v = sql.index("VACUUM (ANALYZE) t;")             # maintenance sits outside any transaction
            self.assertTrue(sql[:v].count("BEGIN;") == sql[:v].count("ROLLBACK;"))
            self.assertTrue((Path(d) / "query.sql").exists())
            self.assertTrue(all(e["file"].startswith("out/") for e in man["experiments"]))
            self.assertEqual(json.loads((Path(d) / "manifest.json").read_text())["baseline"], "out/baseline.json")

    def test_compare_picks_smallest_change_with_nearly_all_the_gain(self):
        import tempfile
        from explain_analyzer.compare import compare_files
        def plan(ms):
            return json.dumps([{"Plan": N("Seq Scan", act=1, total=ms, **{"Relation Name": "t"}), "Execution Time": ms}])
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "base.json").write_text(plan(1000.0))
            (d / "same.json").write_text(plan(1000.0))
            (d / "fast.json").write_text(plan(10.0))
            (d / "bad.json").write_text("")
            text, rows = compare_files([d / "base.json", d / "same.json", d / "fast.json", d / "bad.json"], Config())
        by = {r["label"]: r for r in rows}
        self.assertEqual(by["same.json"]["verdict"], "no change")
        self.assertEqual(by["fast.json"]["verdict"], "FASTER")
        self.assertIn("100.0x faster", text)
        self.assertTrue(by["bad.json"]["verdict"].startswith("FAILED"))

    def test_cli_script_and_text_action_plan(self):
        import io, contextlib, tempfile
        with tempfile.TemporaryDirectory() as d:
            buf, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
                rc = main([str(FIX / "real_nl.json"), "--script", d, "--color", "never"])
            self.assertEqual(rc, 0)
            self.assertIn("== Action plan", buf.getvalue())
            self.assertIn("CREATE STATISTICS", buf.getvalue())
            self.assertTrue((Path(d) / "experiments.sql").exists())
            self.assertIn("--compare", err.getvalue())
