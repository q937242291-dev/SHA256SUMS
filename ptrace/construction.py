"""Source-grounded construction helpers and independently auditable AST rules."""
import ast
import hashlib
import re
from collections import defaultdict, deque
from .audit import structural_errors, endpoint, audit_route
from .privacy import redact_text, sanitize
from .io import digest


def issue_anchor(instance, needle):
    text = instance["problem_statement"]
    start = text.find(needle)
    if start < 0 or not needle:
        return None
    return {"kind": "issue", "start_char": start, "end_char": start + len(needle), "sha256": hashlib.sha256(needle.encode()).hexdigest()}


def python_symbols(snapshot, file):
    try:
        tree = ast.parse(snapshot.text(file))
    except (SyntaxError, ValueError):
        return {}, None
    by_name = defaultdict(list)
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            by_name[n.name].append(n)
    return by_name, tree


def direct_call(snapshot, source, dest):
    """Resolve unambiguous same-file calls between module-level Python functions.
    
    Source anchors cover the call site and callee declaration."""
    if source.get("file") != dest.get("file") or not source.get("file", "").endswith(".py"):
        return None
    file = source["file"]
    by_name, tree = python_symbols(snapshot, file)
    if tree is None:
        return None
    globals_by_name = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    a, b = source.get("symbol"), dest.get("symbol")
    if a not in globals_by_name or b not in globals_by_name or len(by_name[a]) != 1 or len(by_name[b]) != 1:
        return None
    caller, callee = globals_by_name[a], globals_by_name[b]
    bound = {arg.arg for arg in caller.args.posonlyargs + caller.args.args + caller.args.kwonlyargs}
    bound |= {n.id for n in ast.walk(caller) if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del))}
    if caller.args.vararg:
        bound.add(caller.args.vararg.arg)
    if caller.args.kwarg:
        bound.add(caller.args.kwarg.arg)
    if b in bound:
        return None
    # Module rebinding makes even a matching function name ambiguous.
    for n in tree.body:
        if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            if any(isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store) and x.id == b for x in ast.walk(n)):
                return None
    calls = [n for n in ast.walk(caller) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == b]
    if not calls:
        return None
    call = calls[0]
    return [snapshot.anchor(file, call.lineno, getattr(call, "end_lineno", call.lineno)),
            snapshot.anchor(file, callee.lineno, min(callee.lineno + 1, callee.end_lineno))]


def automatic_review(instance, truth, run, route, snapshot, graph=None):
    """Audit a proposed route using revision-specific source bindings."""
    from .artifact_graph import ArtifactGraph as SourceGraph
    from .observed import graph_review
    return graph_review(instance, truth, run, route, graph or SourceGraph(snapshot))


def static_reference_paths(instance, truth, snapshot, limit=3):
    """Enumerate source-verified alternatives through imports and direct calls."""
    from .artifact_graph import ArtifactGraph as SourceGraph
    from .observed import graph_review
    graph = SourceGraph(snapshot)
    result = []
    for route in graph.paths(instance, endpoints=set(truth["target_files"]), limit=limit):
        run = {"run_id": "construction", "observations": []}
        review = graph_review(instance, truth, run, route, graph)
        seen = set()
        for item in review["edge_support"].values():
            for anchor in item["anchors"]:
                if anchor["kind"] != "source":
                    continue
                key = (anchor["file"], anchor["start_line"], anchor["end_line"])
                if key in seen:
                    continue
                seen.add(key)
                run["observations"].append({"step": len(run["observations"]) + 1,
                    "base_commit": instance["base_commit"], "spans": [{"file": key[0],
                    "start_line": key[1], "end_line": key[2], "text": redact_text(snapshot.span(*key))}]})
        if audit_route(route, run, instance, review, snapshot)["valid"] == 1:
            result.append({**route, "source_verified": True, "hop_count": len(route["edges"]), "source_review": review})
    return sanitize(result)


def review_template(instance, run):
    """Create a source-review template for each proposed route."""
    for route in run.get("routes") or []:
        yield {"run_id": run["run_id"], "route_id": route["route_id"], "independent": True, "reviewer_id": "",
               "issue_anchored": None, "endpoint_consistent": None,
               "node_eligibility": {n["node_id"]: None for n in route.get("nodes", [])},
               "edge_support": {e.get("edge_id", str(i)): {"supported": None, "anchors": []} for i, e in enumerate(route.get("edges", []), 1)}}


def annotation_agreement(rows):
    """Compute agreement and Cohen's kappa for observed binary judgments."""
    pairs = [(r["reference"], r["annotation"]) for r in rows if type(r.get("reference")) is bool and type(r.get("annotation")) is bool]
    if not pairs:
        return {"n": 0, "agreement": None, "kappa": None}
    n = len(pairs)
    observed = sum(a == b for a, b in pairs) / n
    pa = sum(a for a, b in pairs) / n
    pb = sum(b for a, b in pairs) / n
    chance = pa*pb + (1-pa)*(1-pb)
    return {"n": n, "agreement": observed, "kappa": (observed-chance)/(1-chance) if chance < 1 else None}
