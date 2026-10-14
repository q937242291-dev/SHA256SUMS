"""Convert same-run generated patches to the official Python SWE-bench schema."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ptrace.io import read_jsonl, write_jsonl, index_unique


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--instances", default="data/instances.jsonl")
    p.add_argument("--runs", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args()
    instances = index_unique(read_jsonl(args.instances))
    result, seen = [], set()
    for run in read_jsonl(args.runs):
        inst = instances[run["instance_id"]]
        if "Pro" in inst["source_dataset"]:
            raise ValueError("Pro tasks require their own official environment; do not pass them to the Python harness")
        if run.get("status") != "completed" or not run.get("model_patch"):
            continue
        if run["base_commit"] != inst["base_commit"]:
            raise ValueError("Patch must belong to the same pre-fix issue run")
        raw = inst["source_instance_id"]
        if raw in seen:
            raise ValueError("Export one source variant per underlying issue and agent/model run")
        seen.add(raw)
        result.append({"instance_id": raw, "model_name_or_path": str(run["agent"])+"/"+str(run["model"]), "model_patch": run["model_patch"]})
    write_jsonl(args.out, result)
    print(f"Exported {len(result)} actual generated patches")


if __name__ == "__main__":
    main()
