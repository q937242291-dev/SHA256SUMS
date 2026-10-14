"""Run a baseline, independent guidance modes, and the paper diagnosis pipeline."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ptrace.adapters import external_run
from ptrace.baselines import run_baseline
from ptrace.io import read_jsonl, index_unique, write_jsonl
from ptrace.llm import LLM
from ptrace.pipeline import diagnose
from ptrace.reporting import paper_tables


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="data")
    parser.add_argument("--cohort", choices=["full", "lite"], default="lite")
    parser.add_argument("--repo-map", default="configs/repo_map.json")
    parser.add_argument("--method", choices=["ptrace", "reference-staged", "reference-structured", "reference-native", "external"], default="ptrace")
    parser.add_argument("--agent-config")
    parser.add_argument("--model-label", default="configured-model")
    parser.add_argument("--provider", choices=["compatible", "anthropic"], default="compatible")
    parser.add_argument("--max-steps", type=int, default=15)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--modes", nargs="+", choices=["check", "trap", "multi"], default=["check", "trap", "multi"])
    parser.add_argument("--baseline", help="Reuse existing canonical baseline runs")
    parser.add_argument("--reviews", help="Use independent source and trace reviews")
    parser.add_argument("--out", default="work/experiment")
    args = parser.parse_args()
    data, out = Path(args.data), Path(args.out)
    lite = json.loads((data / "lite_ids.json").read_text())
    ids = set(lite) if args.cohort == "lite" else None
    instances = [r for r in read_jsonl(data / "instances.jsonl") if ids is None or r["instance_id"] in ids]
    if args.limit:
        instances = instances[:args.limit]
    truths = index_unique(read_jsonl(data / "evaluator.jsonl"))
    repo_map = json.loads(Path(args.repo_map).read_text())
    agent_config = json.loads(Path(args.agent_config).read_text()) if args.agent_config else None
    llm = None if args.method == "external" else LLM(args.provider, args.model_label, args.max_output_tokens)
    if args.method == "external" and not agent_config:
        raise ValueError("External runs require --agent-config")

    def baseline_agent(instance):
        if agent_config:
            return external_run(instance, agent_config, args.model_label)
        return run_baseline(instance, repo_map, args.method, llm, max_steps=args.max_steps)

    def guide_agent(instance, baseline, mode):
        if agent_config:
            return external_run(instance, agent_config, args.model_label, mode, baseline)
        if baseline["agent"] != args.method:
            raise ValueError("Reuse a baseline from the selected method")
        return run_baseline(instance, repo_map, args.method, llm, max_steps=args.max_steps,
                            baseline=baseline, guidance=mode)

    baselines = list(read_jsonl(args.baseline)) if args.baseline else [baseline_agent(i) for i in instances]
    reviews = list(read_jsonl(args.reviews)) if args.reviews else []
    result = diagnose(instances, truths, baselines, guide_agent, repo_map,
                      modes=args.modes, reviews=reviews)
    result.save(out)
    scores = result.baseline_scores + result.guided_scores
    write_jsonl(out / "scores.jsonl", scores)
    paper_tables(scores, lite, out / "tables")
    print(f"Saved {len(result.baseline_scores)} baselines and {len(result.guided_scores)} independent interventions")


if __name__ == "__main__":
    main()
