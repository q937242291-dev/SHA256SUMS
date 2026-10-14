"""Install public baseline sources at an explicitly selected immutable commit."""
import argparse
import re
import subprocess
from pathlib import Path

REPOS = {
    "Agentless": "https://github.com/OpenAutoCoder/Agentless.git",
    "AutoCodeRover": "https://github.com/AutoCodeRoverSG/auto-code-rover.git",
    "OpenHands": "https://github.com/OpenHands/OpenHands.git",
    "SWE-agent": "https://github.com/SWE-agent/SWE-agent.git",
}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--agent", choices=REPOS, required=True)
    p.add_argument("--revision", required=True, help="Exact upstream commit SHA")
    p.add_argument("--directory", required=True)
    args = p.parse_args()
    if not re.fullmatch(r"[a-fA-F0-9]{40}", args.revision):
        raise ValueError("Pin a full commit SHA for reproducible agent behavior")
    destination = Path(args.directory)
    if destination.exists():
        raise ValueError("Use a new destination, avoiding an existing working tree")
    subprocess.run(["git", "clone", "--no-checkout", REPOS[args.agent], str(destination)], check=True)
    subprocess.run(["git", "-C", str(destination), "checkout", "--detach", args.revision], check=True)
    print("Installed pinned official source; follow its own dependency instructions")


if __name__ == "__main__":
    main()
