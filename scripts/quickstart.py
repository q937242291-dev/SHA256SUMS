"""Offline tutorial with a miniature Git repository.

Generated files are written to the selected output directory."""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ptrace.repository import Snapshot
from ptrace.baselines import Tools, run_baseline
from ptrace.construction import automatic_review
from ptrace.metrics import score_run, aggregate
from ptrace.io import write_json, write_jsonl
from ptrace.privacy import sanitize


def main():
    p = argparse.ArgumentParser(); p.add_argument("--out", default="work/tutorial"); args = p.parse_args()
    out = Path(args.out).resolve(); repo = out / "repo"
    if repo.exists():
        raise ValueError("Choose a new tutorial directory")
    repo.mkdir(parents=True)
    code = 'def target(x):\n    return x + 1\n\ndef entry(x):\n    return target(x)\n'
    (repo / "module.py").write_text(code)
    def git(*argv):
        r = subprocess.run(["git", "-C", str(repo), *argv], check=True, capture_output=True, text=True)
        return r.stdout.strip()
    git("init", "-q"); git("add", "module.py")
    git("-c", "user.name=Anonymous", "-c", "user.email=anonymous@example.invalid", "commit", "-qm", "tutorial fixture")
    commit = git("rev-parse", "HEAD")
    inst = {"instance_id": "tutorial-only", "repo": "tutorial/repo", "base_commit": commit,
            "problem_statement": "The entry function in module.py produces the wrong output.", "language": "python"}
    truth = {"instance_id": inst["instance_id"], "target_files": ["module.py"], "category": "Symptom", "hop_group": "1-3"}
    route = {"route_id": "route1", "nodes": [{"node_id": "issue", "kind": "issue"},
             {"node_id": "entry", "kind": "artifact", "file": "module.py", "symbol": "entry"},
             {"node_id": "target", "kind": "artifact", "file": "module.py", "symbol": "target"}],
             "edges": [{"edge_id": "e1", "src": "issue", "dst": "entry", "relation": "mentions"},
                       {"edge_id": "e2", "src": "entry", "dst": "target", "relation": "calls"}]}
    snapshot = Snapshot(repo, commit); tools = Tools(inst, snapshot); tools.read("module.py", 1, 5)
    run = {"run_id": "tutorial-observed-route", "instance_id": inst["instance_id"], "base_commit": commit,
           "agent": "tutorial", "model": "none", "status": "completed", "condition": "baseline", "protocol": "native",
           "access_contract": "pre_fix_source_only_v1", "predicted_files": ["module.py"], "routes": [route],
           "selected_route_id": "route1", "observations": sanitize(tools.observations), "resources": {"tokens": 0, "files": 1, "time_s": 0}, "weight": 1}
    review = automatic_review(inst, truth, run, route, snapshot)
    scores = score_run(inst, truth, run, {(run["run_id"], "route1"): review}, snapshot)
    assert scores["File@5"] == scores["Support@5"] == scores["TV"] == scores["PV@1"] == 1
    assert scores["FC@5"] == 0
    write_jsonl(out / "runs.jsonl", [run]); write_jsonl(out / "reviews.jsonl", [review])
    write_json(out / "summary.json", aggregate([scores]))
    print("Offline tutorial passed: File@5=100, Support@5=100, FC@5=0, TV=100, PV@1=100")


if __name__ == "__main__":
    main()
