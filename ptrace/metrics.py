"""Trajectory metrics and paired analysis for Eqs. (2)-(10)."""
import math
from .io import unique_files, finite_weight, digest
from .audit import audit_route, edge_key
from .privacy import redact_text


def ratio(numerator, denominator):
    return numerator / denominator if denominator > 0 else None


def hit(gold, predicted, k):
    return int(bool(set(predicted[:k]) & set(gold)))


def f1(gold, predicted):
    g, p = set(gold), set(predicted)
    return ratio(2 * len(g & p), len(g) + len(p)) or 0.0


def mrr(gold, predicted):
    return next((1 / i for i, p in enumerate(predicted, 1) if p in gold), 0.0)


def ndcg(gold, predicted, k=10):
    dcg = sum((p in gold) / math.log2(i + 1) for i, p in enumerate(predicted[:k], 1))
    idcg = sum(1 / math.log2(i + 1) for i in range(1, min(len(set(gold)), k) + 1))
    return ratio(dcg, idcg) or 0.0


def evidence_support(audits, predicted, targets, k):
    matched = [a for a in audits if a["endpoint"] in set(predicted[:k]) & set(targets)]
    if any(a["valid"] == 1 for a in matched):
        return 1
    return None if any(a["valid"] is None for a in matched) else 0


def baseline_signature(run):
    return digest({k: run.get(k) for k in ("instance_id", "protocol", "access_contract", "retrieval_contract", "base_commit", "predicted_files", "observations", "routes", "selected_route_id")})


def score_run(instance, truth, run, reviews, snapshot=None, k=5):
    if k < 1:
        raise ValueError("k must be positive")
    if run.get("instance_id") != instance["instance_id"] or truth["instance_id"] != instance["instance_id"]:
        raise ValueError("Issue identity mismatch")
    if run.get("base_commit") != instance["base_commit"]:
        raise ValueError("Base revision mismatch")
    out = {"run_id": run["run_id"], "instance_id": instance["instance_id"], "repo": instance.get("repo"),
           "source_instance_id": instance.get("source_instance_id"), "agent": run.get("agent"),
           "model": run.get("model"), "condition": run.get("condition", "baseline"), "protocol": run.get("protocol"),
           "actual_model": run.get("actual_model"), "agent_revision": run.get("agent_revision"),
           "access_contract": run.get("access_contract"), "base_commit": run["base_commit"], "k": k,
           "status": run.get("status"), "weight": finite_weight(run.get("weight", 1)),
           "baseline_run_id": run.get("baseline_run_id"), "baseline_input_signature": run.get("baseline_input_signature"),
           "input_signature": baseline_signature(run),
           "retrieval_contract_signature": digest(run["retrieval_contract"]) if run.get("retrieval_contract") else None,
           "category": truth.get("verified_category") or truth.get("category"),
           "hop_group": truth.get("verified_hop_group") or truth.get("hop_group"),
           "category_provenance": truth.get("category_provenance"), "hop_group_provenance": truth.get("hop_group_provenance"),
           "file_count_definition": run.get("resources", {}).get("file_count_definition")}
    names = ["File@1", f"File@{k}", "MRR", "NDCG@10", f"FileF1@{k}", "PV@1", "TV", f"Support@{k}", f"FC@{k}",
             "HNegOv", "EdgeF1", "Detour", "Recovery", "RecoveryAfterDetour", "EvidenceRecoveryAfterDetour",
             "Hop1Error", "MeanPosition", "Tokens", "Files", "Time"]
    out.update({name: None for name in names})
    if run.get("status") != "completed":
        return out
    targets = unique_files(truth["target_files"])
    predicted = run.get("predicted_files")
    if predicted is not None:
        predicted = unique_files(predicted)
        out.update({"File@1": hit(targets, predicted, 1), f"File@{k}": hit(targets, predicted, k),
                    "MRR": mrr(targets, predicted), "NDCG@10": ndcg(targets, predicted), f"FileF1@{k}": f1(targets, predicted[:k])})
    routes = run.get("routes")
    audits = []
    if run.get("observations") is not None and routes is not None:
        for route in routes:
            review = reviews.get((run["run_id"], route["route_id"]))
            audits.append(audit_route(route, run, instance, review, snapshot))
        out["TV"] = 1 if any(a["valid"] == 1 for a in audits) else (None if any(a["valid"] is None for a in audits) else 0)
        selected = run.get("selected_route_id")
        if "selected_route_id" in run:
            chosen = [a for a, r in zip(audits, routes) if r["route_id"] == selected]
            out["PV@1"] = chosen[0]["valid"] if len(chosen) == 1 else 0
        if predicted is not None:
            out[f"Support@{k}"] = evidence_support(audits, predicted, targets, k)
            e, s = out[f"File@{k}"], out[f"Support@{k}"]
            out[f"FC@{k}"] = 0 if e == 0 else (1 - s if s is not None else None)
        pe = set()
        for route in routes:
            try:
                pe |= {edge_key(route, e) for e in route.get("edges", [])}
            except (KeyError, TypeError):
                continue
        negative_paths = (truth.get("verified_negative_paths") if truth.get("negative_source_verified") is True
                          else None if truth.get("negative_source_verified") is False
                          else truth.get("hard_negative_paths", []))
        if negative_paths is not None:
            ne = {edge_key(r, e) for r in negative_paths for e in r["edges"]}
            out["HNegOv"] = len(pe & ne) / len(pe) if pe else 0.0
        reference_paths = (truth.get("verified_reference_paths") if truth.get("graph_source_verified") is True
                           else None if truth.get("graph_source_verified") is False
                           else truth.get("reference_paths", []))
        if reference_paths is not None:
            ge = {edge_key(r, e) for r in reference_paths for e in r["edges"]}
            out["EdgeF1"] = f1(ge, pe)
    trace = reviews.get((run["run_id"], "__trace__"))
    if trace and trace.get("independent") is True and trace.get("complete") is True and run.get("observations") is not None:
        steps = {o["step"] for o in run["observations"]}
        detours = trace.get("detour_steps", [])
        if any(d not in steps for d in detours):
            raise ValueError("Audited detour must refer to an observed action")
        delta = min(detours) if detours else None
        out["Detour"] = int(delta is not None)
        returned = False
        for obs in run["observations"]:
            if delta is None or obs["step"] <= delta:
                continue
            if obs.get("base_commit") != instance["base_commit"]:
                continue
            for span in obs.get("spans", []):
                if span.get("file") not in targets or snapshot is None:
                    continue
                try:
                    returned |= redact_text(snapshot.span(span["file"], span["start_line"], span["end_line"])) == span.get("text")
                except (ValueError, KeyError):
                    pass
        out["Recovery"] = int(returned) if snapshot is not None or delta is None else None
        out["RecoveryAfterDetour"] = out["Recovery"] if delta is not None else None
        if delta is not None and audits and predicted is not None:
            before = [audit_route(r, run, instance, reviews.get((run["run_id"], r["route_id"])), snapshot, delta) for r in routes]
            pre = evidence_support(before, predicted, targets, k)
            final = out[f"Support@{k}"]
            out["EvidenceRecoveryAfterDetour"] = int(pre == 0 and final == 1) if pre is not None and final is not None else None
        branch = trace.get("error_route_id")
        errs = [a for a, r in zip(audits, routes or []) if r["route_id"] == branch]
        if errs and errs[0]["first_wrong_hop"] is not None:
            out["MeanPosition"] = errs[0]["first_wrong_hop"]
            out["Hop1Error"] = int(errs[0]["first_wrong_hop"] == 1)
    for key, source in (("Tokens", "tokens"), ("Files", "files"), ("Time", "time_s")):
        value = run.get("resources", {}).get(source)
        if value is not None:
            out[key] = finite_weight(value)
    return out


RATES = {"PV@1", "TV", "HNegOv", "Detour", "Recovery", "RecoveryAfterDetour", "EvidenceRecoveryAfterDetour", "Hop1Error"}


def aggregate(rows):
    names = sorted({k for r in rows for k in r if k in RATES or k.startswith(("File@", "Support@", "FC@")) or k in ("MRR", "NDCG@10", "EdgeF1", "MeanPosition", "Tokens", "Files", "Time") or k.startswith("FileF1@")})
    summary = {}
    for key in names:
        eligible = [r for r in rows if r.get(key) is not None and r.get("weight", 1) > 0]
        denominator = sum(r.get("weight", 1) for r in eligible)
        total = sum(r.get("weight", 1) * r[key] for r in eligible)
        rate = key in RATES or key.startswith(("File@", "Support@", "FC@"))
        summary[key] = {"value": (100 if rate else 1) * total / denominator if denominator else None,
                        "unit": "percent" if rate else ("seconds" if key == "Time" else "raw"),
                        "eligible_n": len(eligible), "weight_sum": denominator, "weighted_sum_raw": total}
    # Identity check must use exactly the same mask and weights.
    cutoffs = [key.split("@")[1] for key in names if key.startswith("Support@")]
    for cutoff in cutoffs:
        keys = [f"File@{cutoff}", f"Support@{cutoff}", f"FC@{cutoff}"]
        common = [r for r in rows if all(r.get(x) is not None for x in keys)]
        for r in common:
            if r[keys[0]] != r[keys[1]] + r[keys[2]]:
                raise ValueError("File = Support + FC identity violated")
        den = sum(r.get("weight", 1) for r in common)
        summary[f"CommonMask@{cutoff}"] = {"eligible_n": len(common), "weight_sum": den,
            **{x: 100 * sum(r.get("weight", 1) * r[x] for r in common) / den if den else None for x in keys}}
        hits = sum(r.get("weight", 1) * r[keys[0]] for r in common)
        false = sum(r.get("weight", 1) * r[keys[2]] for r in common)
        summary[f"FC_over_hits@{cutoff}"] = {"value": 100 * false / hits if hits else None, "unit": "percent", "eligible_n": len(common)}
    return summary


def paired_metrics(baselines, interventions, k=5):
    byid = {b["run_id"]: b for b in baselines}
    if len(byid) != len(baselines):
        raise ValueError("Duplicate baseline run IDs")
    groups, responses, seen = {}, {}, set()
    fc, support = f"FC@{k}", f"Support@{k}"
    for g in interventions:
        b = byid.get(g.get("baseline_run_id"))
        if b is None:
            continue
        if b.get("status") != "completed" or g.get("status") != "completed":
            continue
        if b.get("k") != k or g.get("k") != k:
            raise ValueError("Paired score cutoff differs from the requested cutoff")
        if any(b.get(x) != g.get(x) for x in ("instance_id", "base_commit", "protocol", "access_contract", "retrieval_contract_signature", "k", "agent", "model", "actual_model", "agent_revision", "weight")):
            raise ValueError("Paired issue/protocol/access/cutoff/agent/model/weight mismatch")
        if g.get("baseline_input_signature") != b["input_signature"]:
            raise ValueError("Intervention did not reuse the same baseline input")
        mode = g["condition"]
        pair_id = (b["run_id"], mode)
        if pair_id in seen:
            raise ValueError("Duplicate baseline/mode intervention pair")
        seen.add(pair_id)
        groups.setdefault((g.get("agent"), g.get("model"), mode), []).append((b, g))
        if b.get(fc) == 1 and b.get(support) is not None and g.get(support) is not None and b["status"] == g["status"] == "completed":
            responses.setdefault(b["run_id"], {})[mode] = g[support]
    results = []
    for (agent, model, mode), pairs in groups.items():
        item = {"agent": agent, "model": model, "mode": mode, "pair_n": len(pairs)}
        for key in (f"File@{k}", support, fc, "PV@1", "EdgeF1", "Tokens", "Files", "Time"):
            eligible = [(b, g) for b, g in pairs if b.get(key) is not None and g.get(key) is not None]
            if key == "Files":
                eligible = [(b, g) for b, g in eligible if b.get("file_count_definition") and b["file_count_definition"] == g.get("file_count_definition")]
            den = sum(b["weight"] for b, g in eligible)
            sign = -1 if key == fc else 1
            scale = 100 if key in (f"File@{k}", support, fc, "PV@1") else 1
            item["FCred" if key == fc else "Delta" + key] = scale * sum(b["weight"] * sign * (g[key] - b[key]) for b, g in eligible) / den if den else None
            item[key + "_pair_n"] = len(eligible)
        for key, gain, loss in ((fc, "C_FCred", "C_FCregression"), ("PV@1", "C_PVgain", "C_PVloss")):
            eligible = [(b, g) for b, g in pairs if b.get(key) is not None and g.get(key) is not None]
            good = (1, 0) if key == fc else (0, 1)
            item[gain] = sum((b[key], g[key]) == good for b, g in eligible)
            item[loss] = sum((b[key], g[key]) == good[::-1] for b, g in eligible)
        # Eq. (9): numerator includes every pair in the correction+cost cohort.
        joint = [(b, g) for b, g in pairs if all(r.get(x) is not None for r in (b, g) for x in (fc, "Tokens"))]
        corrections = sum(b[fc] == 1 and g[fc] == 0 for b, g in joint)
        item["TokPerFC"] = ratio(sum(g["Tokens"] - b["Tokens"] for b, g in joint), corrections)
        item["TokPerFC_pair_n"] = len(joint)
        item["TokPerFC_corrections"] = corrections
        results.append(item)
    return results, responses


def response_labels(baselines, responses, tested_modes, k=5):
    labels = []
    for b in baselines:
        modes = set(tested_modes.get(b["run_id"], []))
        observed = responses.get(b["run_id"], {})
        observed = {g: v for g, v in observed.items() if g in modes}
        if b.get(f"FC@{k}") != 1 or not modes:
            label = "inapplicable"
        elif any(v == 1 for v in observed.values()):
            label = "support-responsive"
        elif set(observed) == modes:
            label = "persistent"
        else:
            label = "unresolved"
        labels.append({"instance_id": b["instance_id"], "baseline_run_id": b["run_id"], "label": label,
                       "tested_modes": sorted(modes), "observed_modes": sorted(observed)})
    return labels
