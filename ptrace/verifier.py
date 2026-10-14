"""Auxiliary verifier reranking under an explicit evidence and score contract."""
import copy
import time

from .audit import endpoint
from .io import digest, finite_weight, public_instance, public_route, unique_files
from .metrics import baseline_signature
from .prompts import load_prompt
from .privacy import sanitize


SCORE_FIELDS = {"issue_anchoring", "relation_grounding", "evidence_coverage", "endpoint_explanation"}


def validate_config(config):
    if config.get("evidence_scope") != "baseline_observations":
        raise ValueError("Use baseline_observations as the verifier evidence scope")
    fields, weights = config.get("score_fields", []), config.get("score_weights", {})
    if len(set(fields)) != len(fields) or set(fields) != SCORE_FIELDS or set(weights) != set(fields):
        raise ValueError("Configure all four verifier fields and their weights")
    total = sum(finite_weight(weights[field]) for field in fields)
    if total <= 0:
        raise ValueError("Verifier weights must have a positive total")
    threshold = finite_weight(config.get("acceptance_threshold"))
    if threshold > 1 or type(config.get("max_files")) is not int or config["max_files"] < 1:
        raise ValueError("Use a threshold in [0,1] and a positive max_files")
    return total


def verifier_input(instance, baseline, config):
    """Expose proposed candidates and admitted observations to the verifier."""
    validate_config(config)
    if baseline.get("status") != "completed" or baseline.get("observations") is None:
        raise ValueError("Reranking requires a completed baseline with recorded observations")
    if baseline.get("instance_id") != instance["instance_id"] or baseline.get("base_commit") != instance["base_commit"]:
        raise ValueError("Verifier baseline issue/revision mismatch")
    spans = []
    for observation in baseline["observations"]:
        if observation.get("base_commit") != instance["base_commit"]:
            continue
        for j, span in enumerate(observation.get("spans", [])):
            spans.append({"span_id": f"step-{observation['step']}-span-{j}",
                          "step": observation["step"], "base_commit": instance["base_commit"],
                          "file": span["file"], "start_line": span["start_line"],
                          "end_line": span["end_line"], "text": span["text"]})
    candidates = []
    for file in unique_files(baseline.get("predicted_files") or []):
        routes = [r for r in baseline.get("routes") or [] if endpoint(r) == file]
        candidates.append({"candidate_id": "candidate-" + digest(file)[:16],
                           "file": file, "routes": [public_route(r) for r in routes]})
    return {"issue": public_instance(instance), "configuration": copy.deepcopy(config),
            "candidates": candidates, "source_spans": spans,
            "instruction": load_prompt("verifier_rerank")}


def rank_candidates(payload, response):
    """Calculate configured scores and thresholded rankings from verifier fields."""
    config = payload["configuration"]
    total = validate_config(config)
    allowed = {c["candidate_id"]: c for c in payload["candidates"]}
    span_ids = {s["span_id"] for s in payload["source_spans"]}
    seen, scored = set(), []
    for item in response.get("candidates", []):
        cid = item.get("candidate_id")
        if cid not in allowed or cid in seen:
            raise ValueError("Verifier candidate IDs must be distinct members of the supplied pool")
        seen.add(cid)
        fields = item.get("scores", {})
        values = {field: finite_weight(fields.get(field)) for field in config["score_fields"]}
        if any(v > 1 for v in values.values()):
            raise ValueError("Verifier field scores must be in [0,1]")
        evidence = item.get("evidence_span_ids", [])
        if not set(evidence) <= span_ids:
            raise ValueError("Verifier evidence must refer to admitted source spans")
        if (values["relation_grounding"] > 0 or values["evidence_coverage"] > 0) and not evidence:
            raise ValueError("Positive source-evidence scores require observed span references")
        if not allowed[cid]["routes"]:
            values["relation_grounding"] = values["evidence_coverage"] = 0.0
        score = sum(config["score_weights"][f] * values[f] for f in config["score_fields"]) / total
        scored.append({"candidate_id": cid, "file": allowed[cid]["file"], "score": score,
                       "scores": values, "evidence_span_ids": evidence, "rationale": item.get("rationale", "")})
    scored.sort(key=lambda c: (-c["score"], c["file"]))
    accepted = [c for c in scored if c["score"] >= config["acceptance_threshold"]][:config["max_files"]]
    return scored, [c["file"] for c in accepted]


def run_verifier(instance, baseline, llm, config):
    payload = verifier_input(instance, baseline, config)
    before, started = llm.tokens, time.monotonic()
    from .baselines import json_text
    result = {"run_id": "verify-" + digest([baseline["run_id"], llm.label, llm.model, config])[:20],
              "instance_id": instance["instance_id"], "base_commit": instance["base_commit"],
              "agent": "auxiliary-verifier", "model": llm.label, "actual_model": llm.model,
              "condition": "verifier-rerank", "status": "completed", "weight": baseline.get("weight", 1),
              "protocol": "auxiliary_verifier_reranking", "access_contract": "recorded_context_scoring_v1",
              "baseline_run_id": baseline["run_id"], "baseline_input_signature": baseline_signature(baseline),
              "origin_agent": baseline.get("agent"), "origin_model": baseline.get("model"),
              "origin_protocol": baseline.get("protocol"), "scoring_configuration": copy.deepcopy(config),
              "observations": copy.deepcopy(baseline["observations"]),
              "routes": copy.deepcopy(baseline.get("routes") or []),
              "selected_route_id": None}
    try:
        response = llm.json(load_prompt("verifier_system"), json_text(payload))
        ratings, predicted = rank_candidates(payload, response)
        result.update({"candidate_ratings": ratings, "predicted_files": predicted})
        selected = next((r for r in result["routes"] if predicted and endpoint(r) == predicted[0]), None)
        result["selected_route_id"] = selected["route_id"] if selected else None
    except Exception as exc:
        result.update({"status": "failed", "error_class": type(exc).__name__, "predicted_files": None})
    increment = {"tokens": llm.tokens - before if llm.tokens_observed else None,
                 "time_s": time.monotonic() - started}
    resources = copy.deepcopy(baseline.get("resources", {}))
    for key in ("tokens", "time_s"):
        resources[key] = resources.get(key) + increment[key] if resources.get(key) is not None and increment[key] is not None else None
    result["resources"] = resources
    result["verifier_resources"] = increment
    return sanitize(result)
