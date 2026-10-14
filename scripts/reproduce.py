"""Run the offline end-to-end example for all three guidance modes."""
import argparse
import copy
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ptrace.baselines import run_baseline
from ptrace.construction import automatic_review
from ptrace.io import write_json, write_jsonl
from ptrace.pipeline import diagnose
from ptrace.reporting import paper_tables
from ptrace.repository import Snapshot
from ptrace.source_graph import SourceGraph
from ptrace.dataset import validate


def create_fixture(directory):
    repository = directory / "repo"
    (repository / "app").mkdir(parents=True, exist_ok=True)
    (repository / "app" / "__init__.py").write_text("", encoding="utf-8")
    (repository / "app" / "api.py").write_text(
        "from .core import compute\n\ndef entry(x):\n    return compute(x)\n", encoding="utf-8")
    (repository / "app" / "core.py").write_text(
        "def compute(x):\n    return x + 1\n", encoding="utf-8")
    if not (repository / ".git").exists():
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(["git", "-C", str(repository), "add", "app"], check=True)
    subprocess.run(["git", "-C", str(repository), "-c", "user.name=Anonymous",
                    "-c", "user.email=anonymous@example.invalid", "commit", "--allow-empty", "-qm", "offline example"], check=True)
    commit = subprocess.check_output(["git", "-C", str(repository), "rev-parse", "HEAD"], text=True).strip()
    instance = {"instance_id": "offline-example", "source_instance_id": "offline-example",
                "repo": "example/application", "base_commit": commit,
                "problem_statement": "The entry function returns an incorrect value.", "language": "python"}
    snapshot = Snapshot(repository, commit)
    graph = SourceGraph(snapshot)
    routes = graph.paths(instance, endpoints={"app/core.py"}, limit=3)
    assert routes and any(e["relation"] == "calls" for e in routes[0]["edges"])
    route = routes[0]
    truth = {"instance_id": instance["instance_id"], "target_files": ["app/core.py"],
             "category": "Symptom", "hop_group": "1-3", "reference_paths": [route], "hard_negative_paths": []}
    return instance, truth, route, {instance["repo"]: str(repository.resolve())}


class OfflinePolicy:
    """Deterministic source-tool policy used by the local example."""
    label = "offline-example"
    model = "deterministic-policy"
    tokens = 0
    tokens_observed = True

    def __init__(self, route):
        self.route = route

    def json(self, system, user):
        context = json.loads(user)
        observations = context.get("observations", [])
        files = {span["file"] for obs in observations for span in obs.get("spans", [])}
        if "baseline" not in context:
            if "app/api.py" not in files:
                return {"action": "read", "file": "app/api.py", "start_line": 1, "end_line": 4}
        elif "Accept a supported route" in context["guidance"]:
            return {"action": "finish", "acceptance": "withdraw", "predicted_files": [],
                    "routes": [], "selected_route_id": None}
        elif "app/core.py" not in files:
            return {"action": "read", "file": "app/core.py", "start_line": 1, "end_line": 2}
        return {"action": "finish", "predicted_files": ["app/core.py"],
                "routes": [copy.deepcopy(self.route)], "selected_route_id": self.route["route_id"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="work/example")
    parser.add_argument("--data", default="data")
    args = parser.parse_args()
    counts = validate(args.data)
    print("Dataset validation passed")
    root = Path(__file__).resolve().parents[1]
    subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"], cwd=root, check=True)
    out = Path(args.out).resolve()
    instance, truth, route, repos = create_fixture(out)
    model = OfflinePolicy(route)
    baseline = run_baseline(instance, repos, "ptrace", model)
    assert baseline["status"] == "completed"

    def guide_agent(public, frozen, mode):
        return run_baseline(public, repos, "ptrace", model, baseline=frozen, guidance=mode)

    result = diagnose([instance], [truth], [baseline], guide_agent, repos)
    base = result.baseline_scores[0]
    guided = {s["condition"]: s for s in result.guided_scores}
    assert (base["File@5"], base["Support@5"], base["FC@5"]) == (1, 0, 1)
    assert (guided["check"]["File@5"], guided["check"]["Support@5"], guided["check"]["FC@5"]) == (0, 0, 0)
    assert all(guided[m]["Support@5"] == 1 and guided[m]["FC@5"] == 0 for m in ("trap", "multi"))
    assert result.labels[0]["label"] == "support-responsive"
    result.save(out / "diagnosis")
    write_jsonl(out / "data" / "instances.jsonl", [instance])
    write_jsonl(out / "data" / "evaluator.jsonl", [truth])
    write_json(out / "data" / "lite_ids.json", [instance["instance_id"]])
    write_json(out / "repo_map.json", repos)
    write_jsonl(out / "diagnosis" / "scores.jsonl", result.baseline_scores + result.guided_scores)
    paper_tables(result.baseline_scores + result.guided_scores, [instance["instance_id"]], out / "tables")
    print("Offline example passed: withdrawal, evidence acquisition, paired costs, and response labels")
    print("Outputs: " + str(out))


if __name__ == "__main__":
    main()
