"""Observed-context removal and route-support sensitivity analysis."""
import copy

from .audit import audit_route
from .metrics import evidence_support


def context_ablation(instance, truth, run, reviews, snapshot, k=5):
    """Remove one observation at a time and re-audit complete alternatives."""
    if run.get("observations") is None or run.get("predicted_files") is None:
        return []

    def score(candidate):
        audits = [audit_route(route, candidate, instance,
                             reviews.get((run["run_id"], route["route_id"])), snapshot)
                  for route in candidate.get("routes") or []]
        supported = evidence_support(audits, candidate["predicted_files"], truth["target_files"], k)
        return supported, [route["route_id"] for route, audit in zip(candidate.get("routes") or [], audits)
                           if audit["valid"] == 1]

    original_support, original_routes = score(run)
    rows = []
    for observation in run["observations"]:
        ablated = copy.deepcopy(run)
        ablated["observations"] = [o for o in ablated["observations"] if o["step"] != observation["step"]]
        support, surviving = score(ablated)
        rows.append({"run_id": run["run_id"], "instance_id": instance["instance_id"],
                     "removed_step": observation["step"], "original_support": original_support,
                     "support_after_removal": support,
                     "necessary_for_observed_support": original_support == 1 and support == 0
                     if original_support is not None and support is not None else None,
                     "original_valid_routes": original_routes, "surviving_valid_routes": surviving})
    return rows


def minimal_observed_context(instance, truth, run, reviews, snapshot, k=5):
    """Find a deletion-minimal observed context for the retained route set."""
    candidate = copy.deepcopy(run)
    removed = []
    changed = True
    while changed:
        changed = False
        rows = context_ablation(instance, truth, candidate, reviews, snapshot, k)
        removable = next((r for r in rows if r["original_support"] == 1 and r["support_after_removal"] == 1), None)
        if removable:
            step = removable["removed_step"]
            candidate["observations"] = [o for o in candidate["observations"] if o["step"] != step]
            removed.append(step)
            changed = True
    return {"definition": "deletion_minimal_observed_route_support", "removed_steps": removed,
            "retained_steps": [o["step"] for o in candidate.get("observations") or []],
            "run": candidate}
