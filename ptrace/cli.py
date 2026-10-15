"""Command-line data, run, guidance, source audit and evaluation workflows."""
import argparse
import collections
import json
import sys
from datetime import date
from pathlib import Path
from .io import read_jsonl, write_jsonl, write_json, index_unique, public_instance, digest
from .dataset import validate, select_lite, select_source_lite, build_splits
from .metrics import score_run, aggregate, paired_metrics, response_labels
from .repository import get_snapshot
from .baselines import run_baseline
from .llm import LLM
from .adapters import external_run, import_native
from .candidates import run_selection
from .construction import automatic_review, static_reference_paths, review_template, annotation_agreement
from .reporting import summaries, paper_tables, write_csv
from .privacy import scan, sanitize


def _cohort(args):
    data = Path(args.data)
    ids = set(json.loads((data / "lite_ids.json").read_text())) if args.cohort == "lite" else None
    instances = [r for r in read_jsonl(data / "instances.jsonl") if ids is None or r["instance_id"] in ids]
    if getattr(args, "instance_id", None):
        instances = [r for r in instances if r["instance_id"] == args.instance_id]
    if getattr(args, "limit", None):
        instances = instances[:args.limit]
    if not instances:
        raise ValueError("Selected cohort is empty")
    truths = index_unique(read_jsonl(data / "evaluator.jsonl"))
    return instances, truths


def _json(path, default=None):
    return json.loads(Path(path).read_text(encoding="utf-8")) if path else (default if default is not None else {})


def _model(args):
    return LLM(provider=args.provider, label=args.model_label, max_tokens=args.max_output_tokens)


def cmd_run(args):
    instances, truths = _cohort(args)
    if args.truth:
        truths = index_unique(read_jsonl(args.truth))
    repos = _json(args.repo_map)
    config = _json(args.agent_config)
    if args.method != "external" and args.method != "bm25":
        llm = _model(args)
    else:
        llm = None
    runs = []
    for instance in instances:
        if args.method == "external":
            run = external_run(instance, config, args.model_label)
        elif args.method == "candidate-selection":
            run = run_selection(instance, truths[instance["instance_id"]], llm)
        else:
            run = run_baseline(instance, repos, args.method, llm, args.max_steps, args.max_chars,
                               truth=truths[instance["instance_id"]] if args.method == "oracle-chain" else None)
        runs.append(run)
    write_jsonl(args.out, runs)
    print(f"Wrote {len(runs)} run records")


def cmd_guide(args):
    instances, truths = _cohort(args)
    byinstance = index_unique(instances)
    baselines = list(read_jsonl(args.baseline))
    repos, config = _json(args.repo_map), _json(args.agent_config)
    llm = None if config else _model(args)
    results = []
    for baseline in baselines:
        if baseline["instance_id"] not in byinstance or baseline.get("status") != "completed":
            continue
        if baseline.get("protocol") != "native" or baseline.get("observations") is None:
            raise ValueError("Guidance requires native baseline predictions and actual observations")
        if not config and baseline.get("agent") not in ("reference-staged", "reference-structured", "reference-native", "ptrace"):
            raise ValueError("Official same-agent guidance requires its original configured agent wrapper")
        if config and config["agent"] != baseline["agent"]:
            raise ValueError("Use the same agent configuration for baseline and intervention")
        for mode in args.modes:
            if config:
                result = external_run(byinstance[baseline["instance_id"]], config, args.model_label, mode, baseline)
            else:
                result = run_baseline(byinstance[baseline["instance_id"]], repos, baseline["agent"], llm,
                                      args.max_steps, baseline=baseline, guidance=mode)
            results.append(result)
    write_jsonl(args.out, results)
    print(f"Wrote {len(results)} independent intervention records")


def cmd_evaluate(args):
    instances, truths = _cohort(args)
    byinstance = index_unique(instances)
    if args.truth:
        truths = index_unique(read_jsonl(args.truth))
    runs = list(read_jsonl(args.runs))
    if len({r["run_id"] for r in runs}) != len(runs):
        raise ValueError("Run IDs must be unique; merge only non-overlapping runs")
    reviews = {}
    if args.reviews:
        for r in read_jsonl(args.reviews):
            key = (r["run_id"], r.get("route_id", "__trace__"))
            if key in reviews:
                raise ValueError("Duplicate source review key")
            reviews[key] = r
    repos = _json(args.repo_map)
    snapshots, graphs, scores = {}, {}, []
    for run in runs:
        iid = run["instance_id"]
        if iid not in byinstance:
            continue
        inst = byinstance[iid]
        snapshot = None
        if inst["repo"] in repos:
            key = (inst["repo"], inst["base_commit"])
            if key not in snapshots:
                snapshots[key] = get_snapshot(inst, repos)
            snapshot = snapshots[key]
        if args.auto_audit:
            if snapshot is None:
                raise ValueError("Automatic source audit requires the immutable pre-fix repository")
            from .artifact_graph import ArtifactGraph as SourceGraph
            graph_key = (inst["repo"], inst["base_commit"])
            if graph_key not in graphs:
                graphs[graph_key] = SourceGraph(snapshot)
            for route in run.get("routes") or []:
                key = (run["run_id"], route["route_id"])
                if key not in reviews:
                    reviews[key] = automatic_review(inst, truths[iid], run, route, snapshot, graphs[graph_key])
        score = score_run(inst, truths[iid], run, reviews, snapshot, args.k)
        if "candidate_outcome" in run:
            score["candidate_outcome"] = run["candidate_outcome"]
        scores.append(score)
    out = Path(args.out)
    write_jsonl(out / "scores.jsonl", scores)
    # Aggregate each agent/model/condition/protocol group separately.
    groups = collections.defaultdict(list)
    for row in scores:
        groups[(row["agent"], row["model"], row["condition"], row["protocol"])].append(row)
    report = [{"agent": a, "model": m, "condition": c, "protocol": p, "record_n": len(rr), "metrics": aggregate(rr)}
              for (a,m,c,p), rr in sorted(groups.items(), key=lambda x: str(x[0]))]
    write_json(out / "summary.json", report)
    summaries(scores, out)
    selection = [s for s in scores if "candidate_outcome" in s]
    if selection:
        write_json(out / "candidate_outcomes.json", dict(collections.Counter(s["candidate_outcome"] for s in selection)))
    print(f"Evaluated {len(scores)} records; each metric includes its eligible denominator")


def cmd_pair(args):
    base, guided = list(read_jsonl(args.baseline_scores)), list(read_jsonl(args.guided_scores))
    result, responses = paired_metrics(base, guided, args.k)
    tested = {b["run_id"]: args.modes for b in base}
    out = Path(args.out)
    write_json(out / "paired_summary.json", result)
    write_csv(out / "paired_summary.csv", result)
    write_jsonl(out / "response_labels.jsonl", response_labels(base, responses, tested, args.k))
    print("Wrote paired changes, bidirectional transitions, correction costs and response labels")


def cmd_build(args):
    from .annotation import annotate, patch_spans, base_revision_targets
    from .source_cache import export_source_cache
    instances = list(read_jsonl(args.instances))
    construction = index_unique(read_jsonl(args.construction))
    repos = _json(args.repo_map)
    targets=index_unique(read_jsonl(args.targets)) if args.targets else {}
    source_files=collections.defaultdict(list)
    if args.source_manifest:
        for row in read_jsonl(args.source_manifest):
            source_files[(row['repo'],row['base_commit'])].append(row)
    records, truths = [], []
    for instance in instances:
        snapshot = get_snapshot(instance,repos)
        spans = patch_spans(construction[instance['instance_id']]['patch'])
        available=set(snapshot.files())
        files=(base_revision_targets(targets[instance['instance_id']]['target_files'],construction[instance['instance_id']]['patch'])
               if instance['instance_id'] in targets else sorted({span['file'] for span in spans if span['file'] in available}))
        selected=source_files.get((instance['repo'],instance['base_commit']))
        if selected:
            for row in selected:
                if snapshot.blob_sha(row['file'])!=row['git_blob']:
                    raise ValueError('Source-manifest Git object mismatch')
        truth = annotate(instance,{'target_files':files},snapshot,
                         [span for span in spans if span['file'] in files],
                         negative_limit=max(12,len(targets.get(instance['instance_id'],{}).get('hard_negative_paths',[]))),
                         files=[row['file'] for row in selected] if selected else None)
        truths.append(truth)
        records.append({**instance,**truth})
    lite = select_lite(records) if args.paper_strata else select_source_lite(records)
    byid={r['instance_id']:r for r in records}
    for truth in truths:
        for key in ('category','verified_category','category_provenance'):
            truth[key]=byid[truth['instance_id']][key]
    out = Path(args.out)
    write_jsonl(out / 'issuechain_full.jsonl',records)
    write_jsonl(out / 'instances.jsonl',instances)
    write_jsonl(out / 'evaluator.jsonl',truths)
    write_jsonl(out / 'construction_inputs.jsonl',construction.values())
    write_jsonl(out / "issuechain_lite.jsonl", lite)
    write_json(out / "lite_ids.json", [r["instance_id"] for r in lite])
    write_json(out / "splits.json", build_splits(records))
    span_count=export_source_cache(instances,truths,repos,out/'source_spans.jsonl.gz')
    if args.source_manifest:
        write_jsonl(out / 'source_manifest.jsonl',
                    (row for rows in source_files.values() for row in rows))
    write_json(out / 'dataset_spec.json',{
        'version':'1.3','release_date':date.today().isoformat(),'seed':2027,
        'full_records':len(records),'lite_records':len(lite),
        'unique_source_revision_tasks':len({(r['repo'],r['source_instance_id'],r['base_commit']) for r in records}),
        'repositories':len({r['repo'] for r in records}),
        'reference_path_records':sum(len(r['reference_paths']) for r in records),
        'hard_negative_records':sum(len(r['hard_negative_paths']) for r in records),
        'tasks_with_verified_references':sum(bool(r['reference_paths']) for r in records),
        'lite_categories':dict(collections.Counter(r['category'] for r in lite)),
        'lite_hops':dict(collections.Counter(r['hop_group'] for r in lite)),
        'source_span_records':span_count,
        'source_manifest_records':sum(map(len,source_files.values())),
        'annotation_method':'static_source_binding',
        'source_scope':'manifest' if args.source_manifest else 'repository'})
    print(json.dumps(validate(out), ensure_ascii=False))


def cmd_reference(args):
    instances, truths = _cohort(args)
    repos = _json(args.repo_map)
    updates = []
    for inst in instances:
        truth = truths[inst["instance_id"]]
        paths = static_reference_paths(inst, truth, get_snapshot(inst, repos))
        truth["verified_reference_paths"] = paths
        truth["graph_source_verified"] = bool(paths)
        if paths:
            shortest = min(len(r["edges"]) for r in paths)
            truth["shortest_verified_hops"] = shortest
            truth["verified_hop_group"] = "1-3" if shortest <= 3 else ("4-5" if shortest <= 5 else "6+")
        updates.append(truth)
    write_jsonl(args.out, updates)
    print(f"Wrote {len(updates)} construction records")


def cmd_map(args):
    from .observed import map_run
    instances, truths = _cohort(args)
    byid = index_unique(instances)
    repos = _json(args.repo_map)
    runs, reviews = [], []
    for run in read_jsonl(args.runs):
        if run["instance_id"] in byid:
            instance = byid[run["instance_id"]]
            mapped, audited = map_run(instance, truths[instance["instance_id"]], run, get_snapshot(instance, repos))
            runs.append(mapped)
            reviews.extend(audited)
    write_jsonl(Path(args.out) / "runs.jsonl", runs)
    write_jsonl(Path(args.out) / "reviews.jsonl", reviews)
    print(f"Mapped {len(runs)} observed trajectories")


def cmd_graph(args):
    from .artifact_graph import ArtifactGraph as SourceGraph
    instances, truths = _cohort(args)
    repos = _json(args.repo_map)
    for instance in instances:
        graph = SourceGraph(get_snapshot(instance, repos))
        write_json(Path(args.out) / (instance["instance_id"] + ".json"), graph.export())
    print(f"Indexed {len(instances)} revision-specific graphs")


def cmd_ablate(args):
    from .ablation import context_ablation
    instances, truths = _cohort(args)
    byid, repos = index_unique(instances), _json(args.repo_map)
    reviews = {(r["run_id"], r.get("route_id", "__trace__")): r for r in read_jsonl(args.reviews)}
    rows = []
    for run in read_jsonl(args.runs):
        if run["instance_id"] in byid:
            instance = byid[run["instance_id"]]
            rows.extend(context_ablation(instance, truths[instance["instance_id"]], run,
                                         reviews, get_snapshot(instance, repos), args.k))
    write_jsonl(args.out, rows)
    print(f"Audited {len(rows)} context removals")


def cmd_rerank(args):
    from .verifier import run_verifier
    instances, truths = _cohort(args)
    byid = index_unique(instances)
    config, llm = _json(args.verifier_config), _model(args)
    runs = [run_verifier(byid[r["instance_id"]], r, llm, config)
            for r in read_jsonl(args.baseline) if r["instance_id"] in byid]
    write_jsonl(args.out, runs)
    print(f"Wrote {len(runs)} configured verifier reranking records")


def main(argv=None):
    p = argparse.ArgumentParser(prog="ptrace", description="PTRACE-SWE data, baselines and evaluation")
    sub = p.add_subparsers(dest="command", required=True)
    def data_args(q):
        q.add_argument("--data", default="data")
        q.add_argument("--cohort", choices=["full", "lite"], default="lite")
        q.add_argument("--instance-id")
        q.add_argument("--limit", type=int)
    def model_args(q):
        q.add_argument("--model-label", default="configured-model")
        q.add_argument("--provider", choices=["compatible", "anthropic"], default="compatible")
        q.add_argument("--max-output-tokens", type=int, default=4096)
        q.add_argument("--max-steps", type=int, default=15)
    q = sub.add_parser("validate-data"); q.add_argument("--data", default="data"); q.set_defaults(func=lambda a: print(json.dumps(validate(a.data), ensure_ascii=False)))
    q = sub.add_parser('build-data'); q.add_argument('--instances',required=True); q.add_argument('--construction',required=True)
    q.add_argument('--targets'); q.add_argument('--source-manifest')
    q.add_argument('--repo-map',required=True); q.add_argument('--paper-strata',action='store_true'); q.add_argument('--out',required=True); q.set_defaults(func=cmd_build)
    q = sub.add_parser("run"); data_args(q); model_args(q)
    q.add_argument("--method", required=True, choices=["ptrace", "bm25", "bm25-rerank", "long-context", "oracle-chain", "candidate-selection", "reference-staged", "reference-structured", "reference-native", "external"])
    q.add_argument("--repo-map"); q.add_argument("--agent-config"); q.add_argument("--truth"); q.add_argument("--max-chars", type=int, default=60000); q.add_argument("--out", required=True); q.set_defaults(func=cmd_run)
    q = sub.add_parser("guide"); data_args(q); model_args(q)
    q.add_argument("--baseline", required=True); q.add_argument("--repo-map"); q.add_argument("--agent-config")
    q.add_argument("--modes", nargs="+", choices=["check", "trap", "multi"], default=["check", "trap", "multi"]); q.add_argument("--out", required=True); q.set_defaults(func=cmd_guide)
    q = sub.add_parser("evaluate"); data_args(q); q.add_argument("--runs", required=True); q.add_argument("--reviews"); q.add_argument("--truth"); q.add_argument("--repo-map"); q.add_argument("--auto-audit", action="store_true"); q.add_argument("--k", type=int, default=5); q.add_argument("--out", required=True); q.set_defaults(func=cmd_evaluate)
    q = sub.add_parser("pair"); q.add_argument("--baseline-scores", required=True); q.add_argument("--guided-scores", required=True); q.add_argument("--k", type=int, default=5)
    q.add_argument("--modes", nargs="+", default=["check", "trap", "multi"]); q.add_argument("--out", required=True); q.set_defaults(func=cmd_pair)
    q = sub.add_parser("tables"); q.add_argument("--scores", required=True); q.add_argument("--data", default="data"); q.add_argument("--out", required=True)
    q.set_defaults(func=lambda a: paper_tables(list(read_jsonl(a.scores)), _json(Path(a.data)/"lite_ids.json"), a.out))
    q = sub.add_parser("review-template"); q.add_argument("--runs", required=True); q.add_argument("--out", required=True)
    q.set_defaults(func=lambda a: write_jsonl(a.out, (t for r in read_jsonl(a.runs) for t in review_template({}, r))))
    q = sub.add_parser("build-reference"); data_args(q); q.add_argument("--repo-map", required=True); q.add_argument("--out", required=True); q.set_defaults(func=cmd_reference)
    q = sub.add_parser("build-graph"); data_args(q); q.add_argument("--repo-map", required=True); q.add_argument("--out", required=True); q.set_defaults(func=cmd_graph)
    q = sub.add_parser("map-observations"); data_args(q); q.add_argument("--runs", required=True); q.add_argument("--repo-map", required=True); q.add_argument("--out", required=True); q.set_defaults(func=cmd_map)
    q = sub.add_parser("ablate-context"); data_args(q); q.add_argument("--runs", required=True); q.add_argument("--reviews", required=True); q.add_argument("--repo-map", required=True); q.add_argument("--k", type=int, default=5); q.add_argument("--out", required=True); q.set_defaults(func=cmd_ablate)
    q = sub.add_parser("rerank"); data_args(q); model_args(q); q.add_argument("--baseline", required=True); q.add_argument("--verifier-config", default="configs/verifier.json"); q.add_argument("--out", required=True); q.set_defaults(func=cmd_rerank)
    q = sub.add_parser("agreement"); q.add_argument("--annotations", required=True); q.add_argument("--out", required=True)
    q.set_defaults(func=lambda a: write_json(a.out, annotation_agreement(list(read_jsonl(a.annotations)))))
    q = sub.add_parser("import-native"); data_args(q); q.add_argument("--input", required=True); q.add_argument("--agent", required=True); q.add_argument("--model-label", required=True)
    q.add_argument("--revision", required=True); q.add_argument("--format", choices=["canonical", "sweagent", "agentless"], default="canonical"); q.add_argument("--out", required=True)
    def import_cmd(a):
        instances, truths = _cohort(a)
        if len(instances) != 1:
            raise ValueError("import-native requires --instance-id selecting exactly one issue")
        write_jsonl(a.out, [import_native(a.input, instances[0], a.agent, a.model_label, a.revision, a.format)])
    q.set_defaults(func=import_cmd)
    q = sub.add_parser("privacy-check"); q.add_argument("--root", default=".")
    def check_cmd(a):
        findings = scan(Path(a.root))
        print(json.dumps({"finding_count": len(findings), "findings": findings}, ensure_ascii=False))
        if findings:
            raise ValueError("Remove credential patterns before sharing")
    q.set_defaults(func=check_cmd)
    args = p.parse_args(argv)
    try:
        args.func(args)
    except (ValueError, RuntimeError, OSError) as exc:
        print("Error: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
