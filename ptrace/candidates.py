"""Blinded candidate-chain recognition, separately from native exploration."""
import random
import time
from .io import public_instance, digest, unique_files
from .audit import endpoint
from .baselines import SYSTEM, json_text
from .privacy import sanitize
from .prompts import load_prompt


def make_pool(instance, truth, seed=2027):
    items, labels = [], {}
    all_paths = [(p, "reference") for p in truth["reference_paths"]] + [(p, "distractor") for p in truth["hard_negative_paths"]]
    rng = random.Random(digest([seed, instance["instance_id"]]))
    rng.shuffle(all_paths)
    for i, (p, role) in enumerate(all_paths):
        pid = "p-" + digest([seed, instance["instance_id"], i])[:16]
        mapping = {n["node_id"]: f"n{j}" for j, n in enumerate(p["nodes"])}
        nodes = [{k: v for k, v in n.items() if k in ("kind", "file", "symbol", "value")}
                 | {"node_id": mapping[n["node_id"]]} for n in p["nodes"]]
        edges = [{"edge_id": f"e{j+1}", "src": mapping[e["src"]], "dst": mapping[e["dst"]],
                  "relation": e["relation"]} for j, e in enumerate(p["edges"])]
        items.append({"path_id": pid, "nodes": nodes, "edges": edges})
        labels[pid] = role
    return items, labels


def run_selection(instance, truth, llm, seed=2027):
    pool, labels = make_pool(instance, truth, seed)
    prompt = {"issue": public_instance(instance), "unlabeled_paths": pool,
              "request": load_prompt("candidate_selection")}
    before, started = llm.tokens, time.monotonic()
    obj = llm.json(SYSTEM, json_text(prompt))
    chosen = obj.get("selected_path_id")
    return sanitize({"run_id": "select-" + digest([instance["instance_id"], llm.label, seed])[:20],
                     "instance_id": instance["instance_id"], "base_commit": instance["base_commit"], "agent": "candidate-selection",
                     "model": llm.label, "actual_model": llm.model, "condition": "candidate-recognition", "status": "completed",
                     "protocol": "unlabeled_candidate_paths", "access_contract": "supplied_paths_without_source_v1", "weight": 1,
                     "predicted_files": unique_files(obj.get("predicted_files", [])), "observations": None, "routes": None,
                     "selected_path_id": chosen, "ranked_path_ids": obj.get("ranked_path_ids", []),
                     "candidate_outcome": "invalid" if chosen not in labels else labels[chosen],
                     "resources": {"tokens": llm.tokens-before if llm.tokens_observed else None, "files": 0, "time_s": time.monotonic()-started}})
