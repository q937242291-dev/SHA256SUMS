"""Run the requested official agent/model matrix from local configurations."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--data", default="data")
    p.add_argument("--cohort", choices=["full","lite"], default="lite")
    p.add_argument("--out", default="work/matrix")
    p.add_argument("--limit", type=int)
    p.add_argument("--guided", action="store_true")
    args = p.parse_args()
    if args.guided and args.cohort != "lite":
        p.error("The paper guidance matrix uses --cohort lite")
    cfg = json.loads(Path(args.config).read_text())
    paper = json.loads(Path("configs/experiments.json").read_text())
    for agent, agent_config in cfg["agents"].items():
        models = paper["selected_chain_models"] if agent in paper["selected_chain_agents"] else paper["models"]
        if args.guided:
            if agent != paper["tool_agent"]:
                continue
            models = paper["tool_models"]
        for model in models:
            setting = cfg["models"][model]
            env = os.environ.copy()
            for local, source in setting.get("environment_from", {}).items():
                if source not in env:
                    raise ValueError("Configured process environment variable is unset")
                env[local] = env[source]
            folder = Path(args.out) / agent.replace(" ", "_") / model.replace(" ", "_")
            common = ["--data", args.data, "--cohort", args.cohort, "--agent-config", agent_config, "--model-label", model]
            argv = [sys.executable, "-m", "ptrace", "run", "--method", "external", *common, "--out", str(folder / "baseline.jsonl")]
            if args.limit: argv += ["--limit", str(args.limit)]
            subprocess.run(argv, check=True, env=env)
            if args.guided:
                argv = [sys.executable, "-m", "ptrace", "guide", *common, "--baseline", str(folder / "baseline.jsonl"), "--out", str(folder / "guided.jsonl")]
                subprocess.run(argv, check=True, env=env)
    print("Matrix records saved with actual agent/model provenance")


if __name__ == "__main__":
    main()
