"""Map recorded source observations to complete issue-to-artifact routes."""
from collections import defaultdict

from .audit import anchor_observed, audit_route
from .io import digest
from .artifact_graph import ArtifactGraph as SourceGraph


def observed_routes(instance, run, snapshot, graph=None, prefix=None, limit=100):
    """Apply the observation-to-route map Pi to statically resolved bindings."""
    if run.get("observations") is None:
        return None
    graph = graph or SourceGraph(snapshot)

    def visible(edge):
        return all(anchor_observed(a, run["observations"], instance, snapshot, prefix) is True
                   for a in edge["anchors"])

    return graph.paths(instance, limit=limit, edge_filter=visible)


def graph_review(instance, truth, run, route, graph):
    """Audit a proposal against revision-specific graph relations."""
    nodes = {n["node_id"]: n for n in route.get("nodes", [])}
    eligible, canonical = {}, {}
    for nid, node in nodes.items():
        if node.get("kind") == "issue":
            eligible[nid] = True
        elif node.get("file"):
            key = node["file"] + "::" + (node.get("symbol") or "$module")
            resolved=graph.nodes.get(key)
            if resolved is None:
                value=node.get('value') or node.get('symbol')
                matches=[n for n in graph.nodes.values() if n.get('file')==node['file']
                         and n.get('value')==value and n.get('symbol_kind') in ('literal','option_name')]
                if len(matches)==1:
                    resolved=matches[0]
            canonical[nid]=resolved
            if resolved is not None:
                eligible[nid]=True
            else:
                try:
                    graph.snapshot.text(node['file'])
                    eligible[nid]=None
                except ValueError:
                    eligible[nid]=False
        else:
            eligible[nid] = False
    mentions = {e["dst"]: e for e in graph.issue_edges(instance)}
    support = {}
    for position, edge in enumerate(route.get("edges", []), 1):
        src = canonical.get(edge.get('src')) or nodes.get(edge.get('src'),{})
        dst = canonical.get(edge.get('dst')) or nodes.get(edge.get('dst'),{})
        match = None
        if src.get("kind") == "issue" and edge.get("relation") == "mentions":
            key = dst.get("file", "") + "::" + (dst.get("symbol") or "$module")
            match = mentions.get(key)
        elif src and dst:
            candidates = graph.relation_candidates(src, dst, edge.get("relation"))
            exact = [candidate for candidate in candidates if candidate['edge_id']==edge.get('edge_id')]
            visible = [candidate for candidate in candidates
                       if all(anchor_observed(a,run.get('observations'),instance,graph.snapshot) is True
                              for a in candidate['anchors'])]
            match = next(iter(exact or visible or candidates),None)
        judged = True if match else None
        support[edge.get("edge_id", str(position))] = {
            "supported": judged, "anchors": match["anchors"] if match else [],
            "judgment_source": "revision_static_binding",
        }
    from .audit import endpoint
    return {"run_id": run["run_id"], "route_id": route["route_id"],
            "independent": True, "reviewer_id": "source-graph-v1",
            "issue_anchored": bool(route.get("edges")) and nodes.get(route["edges"][0].get("src"), {}).get("kind") == "issue",
            "endpoint_consistent": endpoint(route) in truth["target_files"],
            "node_eligibility": eligible, "edge_support": support}


def map_run(instance, truth, run, snapshot, limit=100):
    """Extend a trajectory with complete observed alternatives and their audits."""
    graph = SourceGraph(snapshot)
    routes = list(run.get("routes") or [])
    keys = {r["route_id"] for r in routes}
    for route in observed_routes(instance, run, snapshot, graph, limit=limit) or []:
        if route["route_id"] not in keys:
            routes.append(route)
            keys.add(route["route_id"])
    mapped = {**run, "routes": routes if run.get("observations") is not None else None,
              "route_map": "observed_static_bindings_v1"}
    reviews = [graph_review(instance, truth, mapped, r, graph) for r in routes]
    return mapped, reviews


def support_timeline(instance, truth, run, reviews, snapshot, k=5):
    """Score support at each observed prefix, without borrowing future spans."""
    from .metrics import evidence_support
    if run.get("observations") is None or run.get("predicted_files") is None:
        return []
    points = [0] + sorted({o["step"] for o in run["observations"]})
    timeline = []
    for step in points:
        audits = [audit_route(r, run, instance, reviews.get((run["run_id"], r["route_id"])), snapshot, step)
                  for r in run.get("routes") or []]
        support = evidence_support(audits, run["predicted_files"], truth["target_files"], k)
        tv = 1 if any(a["valid"] == 1 for a in audits) else (None if any(a["valid"] is None for a in audits) else 0)
        timeline.append({"step": step, f"Support@{k}": support, "TV": tv})
    return timeline


def context_profile(instance, truth, run, snapshot):
    """Measure reference-relative context and repeated visible line coverage."""
    if run.get("observations") is None or truth.get("graph_source_verified") is not True:
        return None
    reference_lines = defaultdict(set)
    for route in truth.get("verified_reference_paths", []):
        review = route.get("source_review", {})
        for binding in review.get("edge_support", {}).values():
            for anchor in binding.get("anchors", []):
                if anchor.get("kind") == "source":
                    reference_lines[anchor["file"]].update(range(anchor["start_line"], anchor["end_line"] + 1))
    seen, rows = defaultdict(set), []
    for observation in sorted(run["observations"], key=lambda o: o["step"]):
        visible, repeated, relevant = 0, 0, 0
        if observation.get("base_commit") != instance["base_commit"]:
            continue
        for span in observation.get("spans", []):
            from .privacy import redact_text
            try:
                if redact_text(snapshot.span(span["file"], span["start_line"], span["end_line"])) != span["text"]:
                    continue
            except (KeyError, ValueError):
                continue
            lines = set(range(span["start_line"], span["end_line"] + 1))
            visible += len(lines)
            repeated += len(lines & seen[span["file"]])
            relevant += len(lines & reference_lines[span["file"]])
            seen[span["file"]].update(lines)
        rows.append({"step": observation["step"], "visible_lines": visible,
                     "repeated_lines": repeated, "reference_lines": relevant,
                     "outside_reference_lines": visible - relevant})
    return {"definition": "reference_relative_line_coverage", "steps": rows,
            **{name: sum(r[name] for r in rows) for name in
               ("visible_lines", "repeated_lines", "reference_lines", "outside_reference_lines")}}


def trace_review(run, step_judgments, error_route_id=None):
    """Assemble an independent trace audit from explicit per-action judgments."""
    steps = {o["step"] for o in run.get("observations") or []}
    bystep = {r["step"]: r for r in step_judgments}
    if len(bystep) != len(step_judgments) or not set(bystep) <= steps:
        raise ValueError("Trace judgments must identify distinct observed steps")
    complete = set(bystep) == steps and all(
        r.get("independent") is True and r.get("reviewer_id") and type(r.get("detour")) is bool
        for r in bystep.values())
    return {"run_id": run["run_id"], "route_id": "__trace__", "independent": True,
            "complete": complete, "reviewer_id": "trace-audit-" + digest(step_judgments)[:16],
            "detour_steps": sorted(s for s, r in bystep.items() if r.get("detour") is True),
            "error_route_id": error_route_id, "step_judgments": step_judgments}
