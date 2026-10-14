"""Source-grounded route auditing for Eq. (1).

Relation reviews identify semantic bindings, and source anchors identify their
base-revision spans. Recorded observations determine evidence availability."""
import hashlib
from .io import repo_path
from .privacy import redact_text


def edge_key(route, edge):
    nodes = {n["node_id"]: n for n in route.get("nodes", [])}
    def ident(n):
        if n.get("kind") == "issue":
            return ("issue",)
        if n.get("file"):
            symbol = n.get("symbol")
            return ("artifact", repo_path(n["file"]), None if symbol in (None, "", "$module") else symbol)
        return (n.get("kind", "artifact"), n.get("value"))
    return (ident(nodes[edge["src"]]), edge["relation"], ident(nodes[edge["dst"]]))


def structural_errors(route):
    nodes = route.get("nodes", [])
    edges = route.get("edges", [])
    ids = [n.get("node_id") for n in nodes]
    errors = []
    if len(set(ids)) != len(ids) or None in ids:
        errors.append("duplicate_or_missing_node_ids")
    if not edges:
        errors.append("empty_route")
    for edge in edges:
        if edge.get("src") not in ids or edge.get("dst") not in ids or not edge.get("relation"):
            errors.append("invalid_edge")
    if not errors:
        if any(a["dst"] != b["src"] for a, b in zip(edges, edges[1:])):
            errors.append("disconnected_route")
        sequence = [edges[0]["src"]] + [e["dst"] for e in edges]
        if len(set(sequence)) != len(sequence):
            errors.append("cyclic_route")
        byid = {n["node_id"]: n for n in nodes}
        if byid[sequence[0]].get("kind") != "issue":
            errors.append("not_issue_anchored")
        if not byid[sequence[-1]].get("file"):
            errors.append("missing_endpoint_file")
    return sorted(set(errors))


def endpoint(route):
    if structural_errors(route):
        return None
    nodes = {n["node_id"]: n for n in route["nodes"]}
    try:
        return repo_path(nodes[route["edges"][-1]["dst"]]["file"])
    except ValueError:
        return None


def anchor_observed(anchor, observations, instance, snapshot=None, prefix=None):
    if anchor.get("kind") == "issue":
        # The exact issue statement is always visible at step zero.
        text = instance["problem_statement"]
        start, end = anchor.get("start_char"), anchor.get("end_char")
        if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end <= start or end > len(text):
            return False
        return hashlib.sha256(text[start:end].encode()).hexdigest() == anchor.get("sha256")
    if anchor.get("kind") != "source" or anchor.get("base_commit") != instance["base_commit"]:
        return False
    try:
        file = repo_path(anchor["file"])
        start, end = anchor["start_line"], anchor["end_line"]
        if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start:
            return False
        if snapshot is None:
            return None
        text = snapshot.span(file, start, end)
        if hashlib.sha256(text.encode()).hexdigest() != anchor.get("sha256"):
            return False
    except (KeyError, ValueError):
        return False
    # Only actually returned spans count; an open-file name is insufficient.
    # Cover a span by a union of overlapping visible intervals in this file.
    intervals = []
    for o in observations or []:
        if prefix is not None and o.get("step", 0) > prefix:
            continue
        if o.get("base_commit") != instance["base_commit"]:
            continue
        for span in o.get("spans", []):
            if span.get("file") != file:
                continue
            a, b = span.get("start_line"), span.get("end_line")
            if not isinstance(a, int) or not isinstance(b, int):
                continue
            try:
                visible = snapshot.span(file, a, b)
            except ValueError:
                continue
            # Compare each recorded span with its base-revision source text.
            if redact_text(visible) != span.get("text"):
                continue
            intervals.append((a, b))
    cursor = start
    for a, b in sorted(intervals):
        if a > cursor:
            break
        if b >= cursor:
            cursor = b + 1
        if cursor > end:
            return True
    return False


def audit_route(route, run, instance, review, snapshot=None, prefix=None):
    errors = structural_errors(route)
    if errors:
        return {"valid": 0, "endpoint": None, "edge_support": [], "first_wrong_hop": None, "errors": errors}
    result = {"valid": None, "endpoint": endpoint(route), "edge_support": [], "first_wrong_hop": None, "errors": []}
    if run.get("observations") is None:
        result["errors"] = ["observations_unavailable"]
        return result
    if review is None or review.get("independent") is not True or not review.get("reviewer_id"):
        result["errors"] = ["independent_review_missing"]
        return result
    factors = []
    for key in ("issue_anchored", "endpoint_consistent"):
        v = review.get(key)
        factors.append(v if type(v) is bool else None)
    nodes = {n["node_id"]: n for n in route["nodes"]}
    eligible = review.get("node_eligibility", {})
    used = {e["src"] for e in route["edges"]} | {e["dst"] for e in route["edges"]}
    for nodeid in sorted(used):
        v = eligible.get(nodeid)
        if type(v) is not bool:
            factors.append(None)
            continue
        if v and nodes[nodeid].get("file"):
            if snapshot is None:
                v = None
            else:
                try:
                    snapshot.text(nodes[nodeid]["file"])
                except ValueError:
                    v = False
        factors.append(v)
    es = review.get("edge_support", {})
    for hop, edge in enumerate(route["edges"], 1):
        key = edge.get("edge_id", str(hop))
        item = es.get(key)
        supported = None
        if isinstance(item, dict) and type(item.get("supported")) is bool:
            supported = item["supported"]
            if supported:
                anchors = item.get("anchors", [])
                if not anchors:
                    supported = False
                else:
                    checks = [anchor_observed(a, run["observations"], instance, snapshot, prefix) for a in anchors]
                    supported = False if False in checks else (None if None in checks else True)
        result["edge_support"].append(supported)
        factors.append(supported)
        if supported is False and result["first_wrong_hop"] is None:
            result["first_wrong_hop"] = hop
    result["valid"] = 0 if False in factors else (None if None in factors else 1)
    if result["first_wrong_hop"] is not None and None in result["edge_support"][:result["first_wrong_hop"]-1]:
        result["first_wrong_hop"] = None
    if None in factors:
        result["errors"].append("incomplete_source_review")
    return result
