"""Algorithm 1: independent guidance, source audits, and paired diagnosis."""
import copy
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .guidance import MODE_INSTRUCTIONS, transition
from .io import index_unique, public_instance, write_json, write_jsonl
from .metrics import aggregate, paired_metrics, response_labels, score_run
from .observed import graph_review, observed_routes, support_timeline, context_profile
from .artifact_graph import ArtifactGraph as SourceGraph
from .repository import get_snapshot
from .reporting import write_csv


@dataclass
class Diagnosis:
    baselines: list
    interventions: list
    baseline_scores: list
    guided_scores: list
    reviews: list
    paired: list
    labels: list
    transitions: list
    timelines: list
    context_profiles: list

    def save(self, directory):
        directory = Path(directory)
        for name in ("baselines", "interventions", "baseline_scores", "guided_scores",
                     "reviews", "labels", "transitions", "timelines", "context_profiles"):
            write_jsonl(directory / (name + ".jsonl"), getattr(self, name))
        write_json(directory / "paired_summary.json", self.paired)
        write_csv(directory / "paired_summary.csv", self.paired)
        groups = defaultdict(list)
        for row in self.baseline_scores + self.guided_scores:
            groups[(row["agent"], row["model"], row["condition"], row["protocol"])].append(row)
        summary = [{"agent": a, "model": m, "condition": c, "protocol": p,
                    "metrics": aggregate(rows)} for (a, m, c, p), rows in sorted(groups.items())]
        write_json(directory / "summary.json", summary)


def diagnose(instances, truths, baselines, guide_agent, repo_map, modes=("check", "trap", "multi"),
             reviews=(), k=5, map_observations=True):
    """Run independent interventions from frozen baseline inputs.

    guide_agent(public_instance, baseline_copy, mode) returns a canonical run.
    Evaluator truth is used only by the auditing and scoring steps.
    """
    byinstance = index_unique(instances)
    bytruth = index_unique(truths) if not isinstance(truths, dict) else truths
    review_map = {(r["run_id"], r.get("route_id", "__trace__")): r for r in reviews}
    base_scores, guided_scores, interventions = [], [], []
    frozen_baselines, timelines, profiles, tested = [], [], [], {}

    def evaluate(instance, truth, run, snapshot, graph):
        mapped = copy.deepcopy(run)
        if map_observations and run.get("observations") is not None:
            original = {r["route_id"] for r in run.get("routes") or []}
            mapped["routes"] = list(run.get("routes") or [])
            for route in observed_routes(instance, run, snapshot, graph) or []:
                if route["route_id"] not in original:
                    mapped["routes"].append(route)
                    original.add(route["route_id"])
        for route in mapped.get("routes") or []:
            key = (mapped["run_id"], route["route_id"])
            if key not in review_map:
                review_map[key] = graph_review(instance, truth, mapped, route, graph)
        score = score_run(instance, truth, mapped, review_map, snapshot, k)
        timeline = support_timeline(instance, truth, mapped, review_map, snapshot, k)
        timelines.append({"run_id": run["run_id"], "instance_id": instance["instance_id"], "points": timeline})
        profile = context_profile(instance, truth, mapped, snapshot)
        if profile is not None:
            profiles.append({"run_id": run["run_id"], "instance_id": instance["instance_id"], **profile})
        return score

    for baseline in baselines:
        if baseline["instance_id"] not in byinstance:
            continue
        instance, truth = byinstance[baseline["instance_id"]], bytruth[baseline["instance_id"]]
        snapshot = get_snapshot(instance, repo_map)
        graph = SourceGraph(snapshot)
        frozen = copy.deepcopy(baseline)
        frozen_baselines.append(frozen)
        base_score = evaluate(instance, truth, frozen, snapshot, graph)
        # The pairing signature describes the actual input supplied to Guide.
        from .metrics import baseline_signature
        base_score["input_signature"] = baseline_signature(frozen)
        base_scores.append(base_score)
        selected_modes = list(modes.get(frozen["run_id"], ())) if isinstance(modes, dict) else list(modes)
        if len(set(selected_modes)) != len(selected_modes) or any(m not in MODE_INSTRUCTIONS for m in selected_modes):
            raise ValueError("Select distinct check, trap, or multi modes")
        tested[frozen["run_id"]] = selected_modes
        if frozen.get("status") != "completed":
            continue
        for mode in selected_modes:
            guided = guide_agent(public_instance(instance), copy.deepcopy(frozen), mode)
            interventions.append(guided)
            score = evaluate(instance, truth, guided, snapshot, graph)
            guided_scores.append(score)
    pairs, responses = paired_metrics(base_scores, guided_scores, k)
    labels = response_labels(base_scores, responses, tested, k)
    base_byid = {r["run_id"]: r for r in base_scores}
    transitions = [{"run_id": g["run_id"], "baseline_run_id": g["baseline_run_id"],
                    "instance_id": g["instance_id"], "mode": g["condition"],
                    "transition": transition(base_byid[g["baseline_run_id"]], g, k)}
                   for g in guided_scores if g.get("baseline_run_id") in base_byid]
    return Diagnosis(frozen_baselines, interventions, base_scores, guided_scores,
                     list(review_map.values()), pairs, labels, transitions, timelines, profiles)
