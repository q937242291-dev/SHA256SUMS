"""Dataset normalization, Lite sampling, split generation, and validation."""
import collections
import hashlib
import json
import re
import zipfile
from pathlib import Path
from .io import digest, read_jsonl, write_json, write_jsonl, repo_path
from .privacy import sanitize
from .audit import structural_errors

CATEGORY_QUOTAS = {"None": 39, "Configuration": 57, "Lexical": 60, "Security FP": 45, "Structural": 54, "Symptom": 45}
HOP_QUOTAS = {"1-3": 151, "4-5": 89, "6+": 60}
OLD_HOPS = {"short_1_3": "1-3", "mid_4_5": "4-5", "long_6p": "6+"}
PATTERNS = {
    "Configuration": r"\b(config(?:uration)?|setting|option|environment variable|env var|defaults?|ini|yaml|toml|flag)\b",
    "Lexical": r"\b(rename|named?|naming|attribute|symbol|alias|lookup|namespace|parameter|argument|keyword|identifier)\b",
    "Security FP": r"\b(secur(?:e|ity)|vulnerab\w*|cve|xss|csrf|authentication|authorization|permission|injection|password|credential|sanitize|escape|unsafe)\b",
    "Structural": r"\b(inherit\w*|subclass|wrapper|dispatch|import|module|dependency|callback|caller|callee|nested|composition|mixin|interface)\b",
    "Symptom": r"\b(traceback|exception|error|crash|fail\w*|incorrect|wrong|unexpected|regression|reproduce)\b",
}


def category_scores(text):
    # Score issue text against the category mechanism patterns.
    low = text.lower()
    scores = {k: min(10, len(re.findall(p, low))) for k, p in PATTERNS.items()}
    scores["None"] = 2.0 if not any(scores.values()) else 0.0
    scores["Security FP"] *= 1.8
    scores["Configuration"] *= 1.5
    scores["Lexical"] *= 1.1
    return scores


def classify(text):
    scores = category_scores(text)
    return max(CATEGORY_QUOTAS, key=lambda k: (scores[k], -list(CATEGORY_QUOTAS).index(k)))


def opaque_id(legacy_id):
    return "ptx-" + hashlib.sha256(legacy_id.encode()).hexdigest()[:16]


def normalize_path(chain, path_id):
    """Normalize node identities by typed artifact value."""
    nodes, idmap, canonical = [], {}, {}
    for n in chain.get("nodes", []):
        key = (str(n.get("node_type", "artifact")), str(n.get("value", "")))
        if key not in canonical:
            nid = f"n{len(nodes)}"
            canonical[key] = nid
            kind = "issue" if key[0] in ("issue", "issue_span") else "artifact"
            obj = {"node_id": nid, "kind": kind, "artifact_type": key[0], "value": key[1]}
            value = key[1].split("::", 1)[0]
            if "/" in value and re.search(r"\.[A-Za-z0-9]+$", value) and not value.startswith(("http:", "https:")):
                try:
                    obj["file"] = repo_path(value)
                except ValueError:
                    pass
            nodes.append(obj)
        idmap[n["node_id"]] = canonical[key]
    edges = [{"edge_id": f"e{i+1}", "src": idmap.get(e.get("src"), "invalid"),
              "dst": idmap.get(e.get("dst"), "invalid"), "relation": e.get("edge_type", "mentions")}
             for i, e in enumerate(chain.get("edges", []))]
    obj = {"route_id": path_id, "nodes": nodes, "edges": edges, "hop_count": len(edges),
           "source_verified": False, "provenance": "reconstructed_from_supplied_reference",
           "legacy_quality": chain.get("chain_quality"), "negative_mechanism": chain.get("error_mode")}
    obj["structural_flags"] = structural_errors(obj)
    return obj


def _read_zip_jsonl(z, name):
    with z.open(name) as f:
        return [json.loads(l) for l in f if l.strip()]


def rebuild(archive, output):
    out = Path(output)
    with zipfile.ZipFile(archive) as z:
        files = z.namelist()
        suffix = "/IssueChain-Bench-v5/issuechain_core_v5_with_splits.jsonl"
        core = next((n for n in files if n.endswith(suffix)), None)
        if core is None:
            raise ValueError("The supplied v5 full dataset is required")
        records = _read_zip_jsonl(z, core)
        lookup = {}
        for name in files:
            if "/baseline_inputs/swebench_jsonl/" in name and name.endswith(".jsonl"):
                for r in _read_zip_jsonl(z, name):
                    lookup[r["instance_id"]] = r
    instances, evaluator, construction, full = [], [], [], []
    language_by_repo = collections.defaultdict(collections.Counter)
    for r in records:
        if r["language"] != "unknown":
            language_by_repo[r["repo"]][r["language"]]+=1
    # Infer repository language metadata from target-file extension counts.
    extension_votes = collections.defaultdict(collections.Counter)
    extensions = {".py": "python", ".js": "js", ".jsx": "js", ".ts": "ts", ".tsx": "ts", ".go": "go"}
    for r in records:
        for item in r["gold_locations"]:
            if item.get("role") == "root_cause":
                lang = extensions.get(Path(item["file"]).suffix)
                if lang:
                    extension_votes[r["repo"]][lang] += 1
    for r in sorted(records, key=lambda x: x["instance_id"]):
        iid, raw = opaque_id(r["instance_id"]), r["instance_id"].split("::")[-1]
        official = lookup.get(raw)
        problem = r["issue"].get("body") or r["issue"].get("title", "")
        lang = r["language"]
        lang_source = "supplied"
        if lang == "unknown" and language_by_repo[r["repo"]]:
            lang = language_by_repo[r["repo"]].most_common(1)[0][0]
            lang_source = "same_repository_majority"
        elif lang == "unknown" and extension_votes[r["repo"]]:
            lang = extension_votes[r["repo"]].most_common(1)[0][0]
            lang_source = "repository_target_extension_majority"
        inst = {"instance_id": iid, "source_instance_id": raw, "source_dataset": r["source_dataset"],
                "repo": r["repo"], "base_commit": r["snapshot"]["base_commit"], "problem_statement": problem,
                "language": {"js": "javascript", "ts": "typescript"}.get(lang, lang), "language_provenance": lang_source}
        roots = sorted({l["file"] for l in r["gold_locations"] if l.get("role") == "root_cause"})
        if not roots:
            raise ValueError("Record without root-cause targets")
        category = classify(problem)
        hop = OLD_HOPS[r["labels"]["eval_hop_tier"]]
        truth = {"instance_id": iid, "target_files": roots, "target_annotations": r["gold_locations"],
                 "reference_paths": [normalize_path(c, f"ref-{i+1:02}") for i, c in enumerate(r["gold_success_chains"])],
                 "hard_negative_paths": [normalize_path(c, f"neg-{i+1:02}") for i, c in enumerate(r["hard_negative_chains"])],
                 "category": category, "category_provenance": "automatic_keyword_mechanism_nomination",
                 "category_scores": category_scores(problem), "hop_group": hop,
                 "hop_group_provenance": "supplied_legacy_complexity_stratum",
                 "shortest_verified_hops": None, "verified_category": None, "verified_hop_group": None,
                 "graph_source_verified": False, "negative_source_verified": False,
                 "verified_reference_paths": [], "verified_negative_paths": [],
                 "legacy_labels": {k: r["labels"].get(k) for k in ("intent", "issue_type", "security_type", "contamination_risk")}}
        cons = {"instance_id": iid, "source_instance_id": raw, "source_dataset": r["source_dataset"],
                "repo": r["repo"], "base_commit": inst["base_commit"], "source_record_present": official is not None}
        if official:
            for key in ("patch", "test_patch", "version", "FAIL_TO_PASS", "PASS_TO_PASS", "fail_to_pass", "pass_to_pass",
                        "environment_setup_commit", "before_repo_set_cmd", "selected_test_files_to_run", "dockerhub_tag"):
                if key in official:
                    cons[key] = official[key]
        public = sanitize(inst)
        gold = sanitize(truth)
        instances.append(public); evaluator.append(gold); construction.append(sanitize(cons))
        full.append({**public, **gold})
    assert len(instances) == 1547
    assert sum(len(x["reference_paths"]) for x in evaluator) == 4641
    assert sum(len(x["hard_negative_paths"]) for x in evaluator) == 19043
    write_jsonl(out / "issuechain_full.jsonl", full)
    write_jsonl(out / "instances.jsonl", instances)
    write_jsonl(out / "evaluator.jsonl", evaluator)
    write_jsonl(out / "construction_inputs.jsonl", construction)
    write_json(out / "dataset_spec.json", {"version": "1.0", "seed": 2027,
               "lite_category_quotas": CATEGORY_QUOTAS, "lite_hop_quotas": HOP_QUOTAS,
               "source_verified_is_not_inferred_from_legacy_labels": True})
    return full


def select_lite(records, strict=False):
    """Select 300 records with joint category/hop quotas and unique underlying issues.
    
    With strict=True, sampling uses verified_category and verified_hop_group."""
    try:
        import numpy as np
        from scipy.optimize import milp, LinearConstraint, Bounds
        from scipy.sparse import lil_matrix
    except ImportError as exc:
        raise ValueError("Lite resampling needs scipy>=1.11 and numpy; the shipped Lite needs no dependency") from exc
    pool = []
    for r in sorted(records, key=lambda x: x["instance_id"]):
        cat = r.get("verified_category") if strict else r.get("category")
        hop = r.get("verified_hop_group") if strict else r.get("hop_group")
        if cat in CATEGORY_QUOTAS and hop in HOP_QUOTAS:
            pool.append((r, cat, hop))
    if not pool:
        raise ValueError("No eligible records for the selected sampling protocol")
    keys = list(CATEGORY_QUOTAS) + list(HOP_QUOTAS)
    # Duplicate source variants may coexist in full data, but Lite contains each
    # underlying issue at most once.
    duplicate_groups = collections.defaultdict(list)
    for j, (r, cat, hop) in enumerate(pool):
        duplicate_groups[(r["repo"], r["source_instance_id"])].append(j)
    duplicates = [v for v in duplicate_groups.values() if len(v) > 1]
    A = lil_matrix((len(keys) + len(duplicates), len(pool)))
    for j, (r, cat, hop) in enumerate(pool):
        A[keys.index(cat), j] = 1
        A[keys.index(hop), j] = 1
    for i, group in enumerate(duplicates, len(keys)):
        for j in group:
            A[i,j] = 1
    quota = list(CATEGORY_QUOTAS.values()) + list(HOP_QUOTAS.values())
    lower, upper = np.array(quota + [0] * len(duplicates)), np.array(quota + [1] * len(duplicates))
    cost = np.array([int(digest([2027, r["instance_id"]])[:12], 16) / 16**12 for r, cat, hop in pool])
    solution = milp(cost, integrality=np.ones(len(pool)), bounds=Bounds(0, 1),
                    constraints=LinearConstraint(A.tocsr(), lower, upper), options={"time_limit": 60})
    if solution.x is None:
        counts = collections.Counter((cat, hop) for r, cat, hop in pool)
        raise ValueError("Category/hop quotas infeasible; Select a feasible source cohort. " + str(counts))
    selected = [r for (r, cat, hop), x in zip(pool, solution.x) if x > .5]
    if len(selected) != 300:
        raise ValueError("Sampler did not produce an exact feasible cohort")
    return sorted(selected, key=lambda x: x["instance_id"])


def build_splits(instances):
    """Group source variants of the same real issue before supervised splitting."""
    standard = {"train": [], "dev": [], "test": []}
    heldout = {"train": [], "dev": [], "test": []}
    for r in instances:
        group = (r["repo"], r["source_instance_id"])
        number = int(digest([2027, group])[:8], 16) % 100
        part = "train" if number < 70 else ("dev" if number < 85 else "test")
        standard[part].append(r["instance_id"])
        number = int(digest([2027, r["repo"]])[:8], 16) % 100
        part = "train" if number < 70 else ("dev" if number < 85 else "test")
        heldout[part].append(r["instance_id"])
    return {"grouped_standard": standard, "heldout_repository": heldout,
            "note": "Supervised splits group source issues; Full and Lite evaluation uses explicit ID cohorts."}


def validate(data_directory):
    d = Path(data_directory)
    full, lite = list(read_jsonl(d / "issuechain_full.jsonl")), list(read_jsonl(d / "issuechain_lite.jsonl"))
    ids = {r["instance_id"] for r in full}
    if len(ids) != len(full) or len(full) != 1547 or len(lite) != 300:
        raise ValueError("Invalid dataset count or repeated release IDs")
    if not {r["instance_id"] for r in lite} <= ids:
        raise ValueError("Lite must be a subset of full data")
    byid = {r["instance_id"]: r for r in full}
    if any(r != byid[r["instance_id"]] for r in lite):
        raise ValueError("Lite records must be byte-equivalent JSON objects from full data")
    if dict(collections.Counter(r["category"] for r in lite)) != CATEGORY_QUOTAS:
        raise ValueError("Lite category quotas mismatch")
    if dict(collections.Counter(r["hop_group"] for r in lite)) != HOP_QUOTAS:
        raise ValueError("Lite hop quotas mismatch")
    if len({(r["repo"], r["source_instance_id"]) for r in lite}) != 300:
        raise ValueError("Repeated underlying issue in Lite")
    splits = json.loads((d / "splits.json").read_text())
    for name in ("grouped_standard", "heldout_repository"):
        group_to_split = {}
        for split, members in splits[name].items():
            for iid in members:
                r = byid[iid]
                key = r["repo"] if name == "heldout_repository" else (r["repo"], r["source_instance_id"])
                if key in group_to_split and group_to_split[key] != split:
                    raise ValueError("Grouped split leakage")
                group_to_split[key] = split
    return {"full": len(full), "lite": len(lite), "repositories": len({r["repo"] for r in full}),
            "reference_paths": sum(len(r["reference_paths"]) for r in full),
            "hard_negatives": sum(len(r["hard_negative_paths"]) for r in full),
            "lite_categories": dict(collections.Counter(r["category"] for r in lite)),
            "lite_hops": dict(collections.Counter(r["hop_group"] for r in lite))}
