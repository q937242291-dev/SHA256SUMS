"""Source-binding, observation-map, and independent-guidance examples."""
import copy
import hashlib
import unittest
from unittest.mock import patch

from ptrace.audit import edge_key
from ptrace.ablation import context_ablation
from ptrace.construction import static_reference_paths
from ptrace.guidance import guidance_input
from ptrace.metrics import paired_metrics, score_run
from ptrace.observed import graph_review, observed_routes, support_timeline
from ptrace.source_graph import SourceGraph
from ptrace.baselines import run_baseline
from ptrace.candidates import make_pool
from ptrace.privacy import redact_text
from test_paper_metrics import fixture, scored
import test_paper_metrics as metric_fixtures


class SourceSnapshot:
    revision = "a" * 40

    def __init__(self, files):
        self.content = files

    def files(self):
        return sorted(self.content)

    def text(self, file):
        if file not in self.content:
            raise ValueError("Missing artifact")
        return self.content[file]

    def span(self, file, start, end):
        lines = self.text(file).splitlines()
        if start < 1 or end < start or end > len(lines):
            raise ValueError("Invalid span")
        return "\n".join(lines[start-1:end])

    def anchor(self, file, start, end):
        return {"kind": "source", "file": file, "start_line": start, "end_line": end,
                "base_commit": self.revision, "sha256": hashlib.sha256(self.span(file, start, end).encode()).hexdigest()}


def graph_fixture():
    snapshot = SourceSnapshot({
        "pkg/__init__.py": "",
        "pkg/api.py": "from .core import compute as transform\n\ndef entry(x):\n    return transform(x)\n",
        "pkg/core.py": "def compute(x):\n    return x + 1\n",
    })
    instance = {"instance_id": "graph-example", "repo": "example/pkg", "base_commit": snapshot.revision,
                "problem_statement": "entry returns the wrong value."}
    truth = {"instance_id": instance["instance_id"], "target_files": ["pkg/core.py"]}
    graph = SourceGraph(snapshot)
    route = graph.paths(instance, endpoints={"pkg/core.py"})[0]
    observations = [{"step": step, "base_commit": snapshot.revision,
                     "spans": [{"file": file, "start_line": 1,
                                "end_line": len(snapshot.text(file).splitlines()),
                                "text": snapshot.text(file).rstrip("\n")}]}
                    for step, file in enumerate(("pkg/api.py", "pkg/core.py"), 1)]
    run = {"run_id": "graph-run", "instance_id": instance["instance_id"], "base_commit": snapshot.revision,
           "agent": "ptrace", "model": "none", "status": "completed", "protocol": "native",
           "access_contract": "pre_fix_source_only_v1", "predicted_files": ["pkg/core.py"],
           "routes": [route], "selected_route_id": route["route_id"], "observations": observations}
    review = graph_review(instance, truth, run, route, graph)
    return snapshot, instance, truth, graph, run, review


class SourceMethodsTests(unittest.TestCase):
    def test_observed_second_call_site_preserves_early_prefix_support(self):
        snapshot=SourceSnapshot({'pkg/api.py':'from .core import compute as transform\n\ndef entry(x):\n    value=x\n    first=transform(value)\n    return transform(first)\n',
                                 'pkg/core.py':'def compute(x):\n    return x+1\n'})
        instance={'instance_id':'repeated-call','repo':'example/pkg','base_commit':snapshot.revision,
                  'problem_statement':'entry returns an incorrect value.'}
        truth={'target_files':['pkg/core.py']}
        observations=[]
        for step,file,start,end in [(1,'pkg/api.py',1,4),(2,'pkg/api.py',6,6),
                                     (2,'pkg/core.py',1,2),(3,'pkg/api.py',5,5)]:
            observations.append({'step':step,'base_commit':snapshot.revision,
                                 'spans':[{'file':file,'start_line':start,'end_line':end,'text':snapshot.span(file,start,end)}]})
        graph=SourceGraph(snapshot)
        run={'run_id':'repeat','predicted_files':['pkg/core.py'],'observations':observations}
        run['routes']=observed_routes(instance,run,snapshot,graph)
        self.assertEqual(len({r['route_id'] for r in run['routes']}),len(run['routes']))
        reviews={(run['run_id'],r['route_id']):graph_review(instance,truth,run,r,graph) for r in run['routes']}
        timeline=support_timeline(instance,truth,run,reviews,snapshot)
        self.assertEqual(next(row['Support@5'] for row in timeline if row['step']==2),1)

    def test_relative_import_alias_resolves_cross_file_call(self):
        snapshot, instance, truth, graph, run, review = graph_fixture()
        self.assertEqual([e["relation"] for e in run["routes"][0]["edges"]], ["mentions", "calls"])
        score = score_run(instance, truth, run, {(run["run_id"], review["route_id"]): review}, snapshot)
        self.assertEqual((score["Support@5"], score["FC@5"]), (1, 0))

    def test_call_site_without_import_observation_has_no_support(self):
        snapshot, instance, truth, graph, run, review = graph_fixture()
        run["observations"][0]["spans"][0] = {"file": "pkg/api.py", "start_line": 3, "end_line": 4,
                                                  "text": snapshot.span("pkg/api.py", 3, 4)}
        self.assertFalse(any(r["nodes"][-1]["file"] == "pkg/core.py"
                             for r in observed_routes(instance, run, snapshot, graph)))
        score = score_run(instance, truth, run, {(run["run_id"], review["route_id"]): review}, snapshot)
        self.assertEqual(score["Support@5"], 0)

    def test_reading_callee_without_caller_cannot_form_complete_route(self):
        snapshot, instance, truth, graph, run, review = graph_fixture()
        run["observations"] = run["observations"][1:]
        self.assertEqual(observed_routes(instance, run, snapshot, graph), [])

    def test_prefix_route_map_does_not_read_future_callee(self):
        snapshot, instance, truth, graph, run, review = graph_fixture()
        self.assertFalse(any(r["nodes"][-1]["file"] == "pkg/core.py"
                             for r in observed_routes(instance, run, snapshot, graph, prefix=1)))
        self.assertTrue(any(r["nodes"][-1]["file"] == "pkg/core.py"
                            for r in observed_routes(instance, run, snapshot, graph, prefix=2)))

    def test_shadowed_import_is_not_a_resolved_call(self):
        snapshot, instance, truth, graph, run, review = graph_fixture()
        snapshot.content["pkg/api.py"] = "from .core import compute as transform\n\ndef entry(transform):\n    return transform(1)\n"
        graph = SourceGraph(snapshot)
        self.assertFalse(any(e["relation"] == "calls" for e in graph.edges))
        review=graph_review(instance,truth,run,run['routes'][0],graph)
        self.assertIsNone(review['edge_support'][run['routes'][0]['edges'][-1]['edge_id']]['supported'])

    def test_nested_function_call_is_not_attributed_to_outer_function(self):
        snapshot = SourceSnapshot({"a.py": "def target():\n    return 1\n\ndef entry():\n    def inner():\n        return target()\n    return 2\n"})
        self.assertFalse(SourceGraph(snapshot).edges)

    def test_mutable_global_binding_is_not_resolved(self):
        snapshot = SourceSnapshot({"a.py": "def target():\n    return 1\ntarget = other\n\ndef entry():\n    return target()\n"})
        self.assertFalse(SourceGraph(snapshot).edges)

    def test_src_layout_relative_import(self):
        snapshot, instance, truth, graph, run, review = graph_fixture()
        snapshot.content = {"src/" + f: text for f, text in snapshot.content.items()}
        graph = SourceGraph(snapshot)
        self.assertTrue(any(e["relation"] == "calls" for e in graph.edges))

    def test_root_init_file_is_indexed(self):
        self.assertIn("__init__.py::$module", SourceGraph(SourceSnapshot({"__init__.py": ""})).nodes)

    def test_plain_class_method_binding(self):
        snapshot = SourceSnapshot({"a.py": "class Worker:\n    def target(self):\n        return 1\n    def entry(self):\n        return self.target()\n"})
        graph = SourceGraph(snapshot)
        self.assertTrue(any(e["src"] == "a.py::Worker.entry" and e["dst"] == "a.py::Worker.target" for e in graph.edges))

    def test_inherited_method_binding_is_left_unresolved(self):
        snapshot = SourceSnapshot({"a.py": "class Worker(Base):\n    def target(self):\n        return 1\n    def entry(self):\n        return self.target()\n"})
        self.assertFalse(SourceGraph(snapshot).edges)

    def test_relation_identity_ignores_descriptive_prose(self):
        _, _, _, _, run, _ = graph_fixture()
        route = run["routes"][0]
        other = copy.deepcopy(route)
        for node in other["nodes"]:
            node["value"] = "different explanation"
            if node.get("file"):
                node["kind"] = "function"
        self.assertEqual(edge_key(route, route["edges"][1]), edge_key(other, other["edges"][1]))

    def test_timeline_gains_support_at_callee_read(self):
        snapshot, instance, truth, graph, run, review = graph_fixture()
        points = support_timeline(instance, truth, run, {(run["run_id"], review["route_id"]): review}, snapshot)
        self.assertEqual([r["Support@5"] for r in points], [0, 0, 1])

    def test_either_required_read_removal_breaks_support(self):
        snapshot, instance, truth, graph, run, review = graph_fixture()
        rows = context_ablation(instance, truth, run, {(run["run_id"], review["route_id"]): review}, snapshot)
        self.assertTrue(all(r["necessary_for_observed_support"] for r in rows))

    def test_reference_construction_has_actual_source_reviews(self):
        snapshot, instance, truth, graph, run, review = graph_fixture()
        paths = static_reference_paths(instance, truth, snapshot)
        self.assertTrue(paths)
        self.assertTrue(all(p["source_verified"] and p["source_review"]["independent"] for p in paths))

    def test_modes_receive_independent_baseline_copies(self):
        _, instance, _, _, run, _ = graph_fixture()
        inputs = [guidance_input(instance, run, mode, []) for mode in ("check", "trap", "multi")]
        inputs[0]["baseline"]["observations"].clear()
        self.assertEqual(len(inputs[1]["baseline"]["observations"]), 2)
        self.assertEqual(len(run["observations"]), 2)
        self.assertNotIn("target_files", inputs[0]["issue"])

    def test_wrong_revision_cannot_count_as_target_recovery(self):
        instance, truth, run, review, snapshot = fixture()
        run["observations"] = [{"step": 1, "base_commit": instance["base_commit"], "spans": []},
                               {**run["observations"][0], "step": 2, "base_commit": "b" * 40}]
        score = scored(instance, truth, run, review, snapshot,
                       {"independent": True, "complete": True, "detour_steps": [1]})
        self.assertEqual(score["RecoveryAfterDetour"], 0)

    def test_failed_pair_is_excluded_even_with_stale_fields(self):
        helper = metric_fixtures.PairedTests()
        baseline = helper.base()
        guided = helper.guide(baseline, status="failed", **{"FC@5": 0, "Support@5": 1})
        pairs, responses = paired_metrics([baseline], [guided])
        self.assertEqual(pairs, [])
        self.assertEqual(responses, {})

    def test_unverified_graphs_do_not_enter_relation_metrics(self):
        instance, truth, run, review, snapshot = fixture()
        truth.update({"graph_source_verified": False, "negative_source_verified": False,
                      "reference_paths": run["routes"], "hard_negative_paths": run["routes"]})
        score = scored(instance, truth, run, review, snapshot)
        self.assertIsNone(score["EdgeF1"])
        self.assertIsNone(score["HNegOv"])
        self.assertEqual(score["Support@5"], 1)

    def test_candidate_nodes_withhold_annotation_fields(self):
        snapshot, instance, truth, graph, run, review = graph_fixture()
        path = copy.deepcopy(run["routes"][0])
        path["nodes"][-1].update({"label": "gold", "source_verified": True, "artifact_type": "code_fix_files"})
        truth.update({"reference_paths": [path], "hard_negative_paths": []})
        pool, labels = make_pool(instance, truth)
        self.assertNotIn("label", pool[0]["nodes"][-1])
        self.assertNotIn("source_verified", pool[0]["nodes"][-1])
        self.assertNotIn("artifact_type", pool[0]["nodes"][-1])

    def test_staged_structured_and_context_baselines_record_source(self):
        snapshot, instance, truth, graph, run, review = graph_fixture()
        route = run["routes"][0]

        class Policy:
            tokens, tokens_observed = 0, True
            label, model = "offline-example", "deterministic-policy"

            def json(self, system, user):
                self.tokens += 5
                if "at file level" in system:
                    return {"predicted_files": ["pkg/api.py", "pkg/core.py"]}
                if "outgoing calls" in system:
                    return {"artifacts": [{"file": "pkg/api.py", "symbol": "entry"}]}
                if "visible declarations" in system:
                    return {"locations": [{"file": "pkg/api.py", "start_line": 1, "end_line": 4}]}
                return {"action": "finish", "predicted_files": ["pkg/core.py"],
                        "routes": [copy.deepcopy(route)], "selected_route_id": route["route_id"]}

        for method in ("reference-staged", "reference-structured", "bm25-rerank", "long-context"):
            with self.subTest(method=method), patch("ptrace.baselines.get_snapshot", return_value=snapshot):
                result = run_baseline(instance, {}, method, Policy())
                self.assertEqual(result["status"], "completed")
                source_review = graph_review(instance, truth, result, route, graph)
                score = score_run(instance, truth, result, {(result["run_id"], route["route_id"]): source_review}, snapshot)
                self.assertEqual(score["Support@5"], 1)
                self.assertGreater(result["resources"]["tokens"], 0)

    def test_public_source_urls_survive_path_redaction(self):
        text = "https://github.com/example/project/issues/123"
        self.assertEqual(redact_text(text), text)
        self.assertEqual(redact_text("C:/Users/example/project"), "[REDACTED_LOCAL_PATH]")
        self.assertEqual(redact_text("/home/example/project"), "[REDACTED_LOCAL_PATH]")


if __name__ == "__main__":
    unittest.main()
