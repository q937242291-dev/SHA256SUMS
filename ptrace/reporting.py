"""Aggregate scores into experiment tables and figure inputs."""
import collections
import csv
import json
from pathlib import Path
from .metrics import aggregate, paired_metrics
from .io import write_json


def write_csv(path, rows):
    p = Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    keys = list(dict.fromkeys(k for row in rows for k in row))
    with p.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader(); writer.writerows(rows)


def summaries(scores, out):
    out = Path(out)
    groups = collections.defaultdict(list)
    for s in scores:
        groups[(s.get("agent"), s.get("model"), s.get("condition"), s.get("protocol"))].append(s)
    flattened = []
    for (agent, model, condition, protocol), rows in sorted(groups.items(), key=lambda x: str(x[0])):
        summary = aggregate(rows)
        item = {"agent": agent, "model": model, "condition": condition, "protocol": protocol, "record_n": len(rows)}
        for metric, value in summary.items():
            if "value" in value:
                item[metric] = value["value"]
                item[metric + "_n"] = value["eligible_n"]
        common = [r for r in rows if r.get("TV") is not None and r.get("Support@5") is not None]
        den = sum(r.get("weight", 1) for r in common)
        item["TV_minus_Support_pp"] = 100 * sum(r.get("weight", 1) * (r["TV"] - r["Support@5"]) for r in common) / den if den else None
        item["TV_minus_Support_n"] = len(common)
        flattened.append(item)
    write_csv(out / "agent_summary.csv", flattened)
    strata = []
    groups = collections.defaultdict(list)
    for s in scores:
        for axis, key in (("category", "category"), ("hop", "hop_group")):
            if s.get(key) is not None:
                groups[(s.get("agent"), s.get("model"), s.get("condition"), s.get("protocol"), axis, s[key])].append(s)
    for (agent, model, condition, protocol, axis, stratum), rows in sorted(groups.items(), key=lambda x: str(x[0])):
        summary = aggregate(rows)
        item = {"agent": agent, "model": model, "condition": condition, "protocol": protocol, "axis": axis, "stratum": stratum, "n": len(rows)}
        for metric, value in summary.items():
            if "value" in value:
                item[metric] = value["value"]
                item[metric + "_n"] = value["eligible_n"]
        strata.append(item)
    write_csv(out / "strata.csv", strata)
    return flattened, strata


def paper_tables(scores, lite_ids, out):
    """Export paper-scope tables from per-run metric records."""
    out = Path(out); out.mkdir(parents=True, exist_ok=True)
    config = json.loads((Path(__file__).resolve().parents[1] / "configs/experiments.json").read_text())
    lite_ids = set(lite_ids)
    base = [s for s in scores if s.get("condition") == "baseline"]
    native = [s for s in base if s.get("agent") in config["native_agents"] and s.get("model") in config["models"] and s.get("protocol") == "native"]
    selected_chains = [s for s in base if s.get("agent") in config["selected_chain_agents"] and s.get("model") in config["selected_chain_models"]]
    full = native + selected_chains
    lite = [s for s in native if s["instance_id"] in lite_ids]
    full_summary, _ = summaries(full, out / "full")
    lite_summary, strata = summaries(lite, out / "lite")
    write_csv(out / "table1_full_agents.csv", full_summary)
    write_csv(out / "table5_lite_agents.csv", lite_summary)
    hit_rows = []
    groups = collections.defaultdict(list)
    for s in full:
        groups[(s["agent"], s["model"])].append(s)
    for (agent, model), group in groups.items():
        # Unweighted counts are labelled separately from metric averages.
        if agent in config["selected_chain_agents"]:
            available = [s for s in group if s.get("File@5") is not None]
            hit_rows.append({"agent": agent, "model": model, "eligible_n": len(available),
                             "hits": sum(s["File@5"] for s in available), "false_credit": None,
                             "supported_hits": None})
            continue
        common = [s for s in group if all(s.get(k) is not None for k in ("File@5", "FC@5", "Support@5"))]
        hit_rows.append({"agent": agent, "model": model, "eligible_n": len(common),
                         "hits": sum(s["File@5"] for s in common), "false_credit": sum(s["FC@5"] for s in common),
                         "supported_hits": sum(s["Support@5"] for s in common)})
    write_csv(out / "table4_hit_counts.csv", hit_rows)
    write_csv(out / "table6_recovery.csv", [{k: r.get(k) for k in ("agent", "model", "Detour", "Detour_n", "Recovery", "Recovery_n", "RecoveryAfterDetour", "RecoveryAfterDetour_n")} for r in lite_summary if r["agent"] == config["recovery_agent"]])
    error_groups = collections.defaultdict(list)
    for s in lite:
        if s.get("agent") in config["error_agents"] and s.get("MeanPosition") is not None:
            error_groups[(s["agent"], s["model"], s["category"])].append(s)
    errors = []
    for (agent, model, category), rows in error_groups.items():
        summary = aggregate(rows)
        errors.append({"agent": agent, "model": model, "category": category, "n": len(rows),
                       "H1": sum(s["MeanPosition"] == 1 for s in rows), "H2": sum(s["MeanPosition"] == 2 for s in rows),
                       "H3": sum(s["MeanPosition"] == 3 for s in rows), "H4plus": sum(s["MeanPosition"] >= 4 for s in rows),
                       "Hop1Error": summary["Hop1Error"]["value"], "MeanPosition": summary["MeanPosition"]["value"]})
    write_csv(out / "table7_first_errors.csv", errors)
    context = [s for s in base if s.get("agent") in config["context_methods"]]
    table8, _ = summaries(context, out / "context")
    write_csv(out / "table8_context_inputs.csv", table8)
    selection = [s for s in scores if s.get("agent") == config["candidate_agent"] and s.get("model") == config["candidate_model"] and s["instance_id"] in lite_ids]
    table9, _ = summaries(selection, out / "candidate")
    for row in table9:
        observations = [s for s in selection if all(s.get(k) == row.get(k) for k in ("agent", "model", "condition", "protocol"))
                        and s.get("status") == "completed" and s.get("candidate_outcome") in ("reference", "distractor", "invalid")]
        row["recognition_n"] = len(observations)
        for label, column in (("reference", "Correct"), ("distractor", "Distractor"), ("invalid", "Invalid")):
            row[column] = sum(s["candidate_outcome"] == label for s in observations) if observations else None
    write_csv(out / "table9_candidate_recognition.csv", table9)
    tool_base = [s for s in lite if s["agent"] == config["tool_agent"] and s["model"] in config["tool_models"]]
    guided = [s for s in scores if s.get("condition") in config["tool_modes"] and s.get("agent") == config["tool_agent"] and s.get("model") in config["tool_models"] and s["instance_id"] in lite_ids and s.get("protocol") == "native"]
    pairs, responses = paired_metrics(tool_base, guided, 5)
    write_csv(out / "table2_guidance.csv", pairs)
    write_csv(out / "table10_transitions.csv", pairs)
    categories = []
    for category in ("None", "Configuration", "Lexical", "Security FP", "Structural", "Symptom"):
        bs, gs = [s for s in tool_base if s.get("category") == category], [s for s in guided if s.get("category") == category]
        rows, _ = paired_metrics(bs, gs, 5)
        categories.extend({"category": category, **r} for r in rows)
    variants = config["category_variants"]
    selected = [r for r in categories if variants.get(r["model"], {}).get(r["category"]) == r["mode"]]
    for item in selected:
        bs = {b["run_id"]: b for b in tool_base if b["agent"] == item["agent"] and b["model"] == item["model"] and b.get("category") == item["category"]}
        pp = [(bs[g["baseline_run_id"]],g) for g in guided if g.get("baseline_run_id") in bs and g["condition"] == item["mode"]]
        common = [(b,g) for b,g in pp if all(r.get(k) is not None for r in (b,g) for k in ("File@5","FC@5","Support@5"))]
        item["common_outcome_pair_n"] = len(common)
        item["DeltaFile_count"] = sum(g["File@5"]-b["File@5"] for b,g in common)
        item["FCred_count"] = sum(b["FC@5"]-g["FC@5"] for b,g in common)
        item["DeltaSupport_count"] = sum(g["Support@5"]-b["Support@5"] for b,g in common)
        item["DeltaPV_count"] = sum(g["PV@1"]-b["PV@1"] for b,g in pp if b.get("PV@1") is not None and g.get("PV@1") is not None)
    write_csv(out / "table2_category_variants.csv", selected)
    write_csv(out / "table11_category_balances.csv", selected)
    gains = []
    # Use the experiment configuration to select model/category variants.
    selected_groups = collections.defaultdict(list)
    for r in selected:
        selected_groups[(r["agent"],r["model"])].append(r)
    for (agent, model), group in selected_groups.items():
        for include_none in (False, True):
            rr = [r for r in group if include_none or r["category"] != "None"]
            gains.append({"agent": agent, "model": model, "category_scope": "6 incl None" if include_none else "5 named", "observed_categories": len(rr),
                          **{name: sum(r.get(key) is not None and r[key] > 0 for r in rr) for name, key in (("PV_gain", "DeltaPV@1"), ("FC_reduction", "FCred"), ("File_gain", "DeltaFile@5"), ("Edge_gain", "DeltaEdgeF1"), ("Support_gain", "DeltaSupport@5"))}})
    write_csv(out / "table12_category_gains.csv", gains)
    return pairs


def _group_categories(rows):
    out = collections.defaultdict(list)
    for r in rows:
        out[(r["agent"], r["model"], r["mode"])].append(r)
    return out


def paired_bootstrap(pairs, metric="Support@5", replicates=2000, seed=2027):
    """Optional issue-level paired bootstrap; same-issue source variants group."""
    import random
    eligible = [(b, g) for b, g in pairs if b.get(metric) is not None and g.get(metric) is not None]
    if not eligible:
        return {"n": 0, "low": None, "high": None}
    rng = random.Random(seed)
    clusters = collections.defaultdict(list)
    for b, g in eligible:
        clusters[(b.get("repo"), b.get("source_instance_id") or b["instance_id"])].append((b,g))
    keys = list(clusters)
    means = []
    for _ in range(replicates):
        sample = [pair for _ in keys for pair in clusters[rng.choice(keys)]]
        den = sum(b["weight"] for b, g in sample)
        if den:
            means.append(sum(b["weight"] * (g[metric]-b[metric]) for b, g in sample) / den)
    means.sort()
    if not means:
        return {"n": len(eligible), "low": None, "high": None, "seed": seed}
    return {"n": len(eligible), "clusters": len(keys), "low": means[int(.025*(len(means)-1))], "high": means[int(.975*(len(means)-1))], "seed": seed}
