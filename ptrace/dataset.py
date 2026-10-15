"""Dataset sampling, split generation, and source-grounded validation."""
import collections
import hashlib
import json
import re
from pathlib import Path
from .io import digest, read_jsonl, write_json, write_jsonl, repo_path
from .audit import structural_errors, endpoint

CATEGORY_QUOTAS = {"None": 39, "Configuration": 57, "Lexical": 60, "Security FP": 45, "Structural": 54, "Symptom": 45}
HOP_QUOTAS = {"1-3": 151, "4-5": 89, "6+": 60}


def artifact_location(value):
    """Read repository file and qualified-symbol identifiers from artifact text."""
    parts = value.split("::")
    candidate = parts[0]
    extensions = (".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".go", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".md", ".rst", ".txt", ".sh")
    if not candidate.endswith(extensions) or any(c.isspace() for c in candidate) or candidate.startswith(("http:", "https:")):
        return {}
    try:
        file = repo_path(candidate)
    except ValueError:
        return {}
    result = {"file": file}
    if len(parts) > 1 and all(re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", p) for p in parts[1:]):
        result["symbol"] = ".".join(parts[1:])
    return result


def select_lite(records, strict=True):
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


def select_source_lite(records, size=300):
    """Balance a fixed-size subset using source-verified labels and routes."""
    eligible = [r for r in records if r.get('graph_source_verified') is True
                and r.get('verified_category') in CATEGORY_QUOTAS
                and r.get('verified_hop_group') in HOP_QUOTAS]
    unique = {}
    for row in sorted(eligible,key=lambda r:(-len(r['reference_paths']),digest([2027,r['instance_id']]))):
        unique.setdefault((row['repo'],row['source_instance_id']),row)
    pool = list(unique.values())
    if len(pool) < size:
        raise ValueError('Insufficient source-verified issues for the subset')
    available = collections.Counter(r['hop_group'] for r in pool)
    if size==300:
        try:
            import numpy as np
            from scipy.optimize import milp, LinearConstraint, Bounds
            from scipy.sparse import lil_matrix
            options=[]
            for i,row in enumerate(pool):
                categories={row['verified_category']} | {e['category'] for e in row.get('category_evidence',[])}
                for category in sorted(categories):
                    if category in CATEGORY_QUOTAS:
                        options.append((i,category))
            categories=list(CATEGORY_QUOTAS)
            matrix=lil_matrix((len(categories)+len(pool),len(options)))
            costs=[]
            for j,(i,category) in enumerate(options):
                row=pool[i]
                matrix[categories.index(category),j]=1
                matrix[len(categories)+i,j]=1
                costs.append((len(row['reference_paths'])<3)*10 + available[row['hop_group']]/len(pool)
                             + int(digest([2027,row['instance_id'],category])[:8],16)/16**8/100)
            quotas=list(CATEGORY_QUOTAS.values())
            result=milp(np.array(costs),integrality=np.ones(len(options)),bounds=Bounds(0,1),
                        constraints=LinearConstraint(matrix.tocsr(),np.array(quotas+[0]*len(pool)),
                                                     np.array(quotas+[1]*len(pool))),
                        options={'time_limit':30})
            if result.x is not None:
                selected=[]
                for (i,category),value in zip(options,result.x):
                    if value>.5:
                        row=pool[i]
                        row['category']=row['verified_category']=category
                        row['category_provenance']='source_binding_stratum'
                        selected.append(row)
                if len(selected)==size:
                    return sorted(selected,key=lambda r:r['instance_id'])
        except ImportError:
            pass
    def priority(row):
        return (len(row['reference_paths']) < 3, available[row['hop_group']], digest([2027,row['instance_id']]))
    selected = []
    for category, quota in CATEGORY_QUOTAS.items():
        candidates = sorted((r for r in pool if r['category']==category),key=priority)
        selected.extend(candidates[:round(quota*size/300)])
    chosen = {r['instance_id'] for r in selected}
    for row in sorted((r for r in pool if r['instance_id'] not in chosen),key=priority):
        if len(selected)>=size:
            break
        selected.append(row)
    return sorted(selected[:size],key=lambda r:r['instance_id'])


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
    spec = json.loads((d / 'dataset_spec.json').read_text())
    ids = {r["instance_id"] for r in full}
    if (len(ids) != len(full) or len(full) != spec.get('full_records',1547)
            or len(lite) != spec.get('lite_records',300)
            or len({r['instance_id'] for r in lite}) != len(lite)):
        raise ValueError("Invalid dataset count or repeated release IDs")
    if not {r["instance_id"] for r in lite} <= ids:
        raise ValueError("Lite must be a subset of full data")
    byid = {r["instance_id"]: r for r in full}
    if any(r != byid[r["instance_id"]] for r in lite):
        raise ValueError("Lite records must be byte-equivalent JSON objects from full data")
    if dict(collections.Counter(r["category"] for r in lite)) != spec['lite_categories']:
        raise ValueError("Lite category quotas mismatch")
    if dict(collections.Counter(r["hop_group"] for r in lite)) != spec['lite_hops']:
        raise ValueError("Lite hop quotas mismatch")
    if len({(r["repo"], r["source_instance_id"]) for r in lite}) != len(lite):
        raise ValueError("Repeated underlying issue in Lite")
    instances={r['instance_id']:r for r in read_jsonl(d/'instances.jsonl')}
    truths={r['instance_id']:r for r in read_jsonl(d/'evaluator.jsonl')}
    if instances.keys()!=ids or truths.keys()!=ids:
        raise ValueError('Public and evaluator views must cover the Full IDs')
    if any({**instances[iid],**truths[iid]}!=row for iid,row in byid.items()):
        raise ValueError('Public/evaluator views disagree with the Full record')
    for row in full:
        for key in ('reference_paths','hard_negative_paths'):
            for route in row[key]:
                if structural_errors(route) or route.get('source_verified') is not True:
                    raise ValueError('Invalid source route in '+row['instance_id'])
                if route['hop_count'] != len(route['edges']):
                    raise ValueError('Relation-count mismatch')
                if key=='reference_paths' and endpoint(route) not in row['target_files']:
                    raise ValueError('Reference endpoint mismatch')
        if row.get('graph_source_verified'):
            shortest = min(p['hop_count'] for p in row['reference_paths'])
            if shortest!=row['shortest_verified_hops']:
                raise ValueError('Shortest reference-route mismatch')
    if any(not r.get('graph_source_verified') or r.get('category')!=r.get('verified_category')
           or r.get('hop_group')!=r.get('verified_hop_group') for r in lite):
        raise ValueError('Lite requires source-verified paths and labels')
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
