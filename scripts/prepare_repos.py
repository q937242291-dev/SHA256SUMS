"""Clone benchmark repositories and fetch the selected pre-fix commits."""
import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ptrace.io import read_jsonl, write_json


def git(directory, *args, check=True):
    command = ["git"] + (["-C", str(directory)] if directory else []) + list(args)
    process = subprocess.run(command, capture_output=True, text=True)
    if check and process.returncode:
        raise RuntimeError("Git command failed: " + " ".join(args[:2]))
    return process


def prepare(repository, commits, root, fetch=True):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Repository must be an owner/name identifier")
    directory = root / repository.replace("/", "__")
    if not directory.exists():
        if not fetch:
            raise ValueError("Repository is absent: " + repository)
        git(None, "clone", "--filter=blob:none", "--no-checkout",
            "https://github.com/" + repository + ".git", str(directory))
    for commit in sorted(commits):
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", commit):
            raise ValueError("Invalid pre-fix commit SHA")
        available = git(directory, "cat-file", "-e", commit + "^{commit}", check=False).returncode == 0
        if not available:
            if not fetch:
                raise ValueError("Missing pre-fix object in " + repository)
            git(directory, "fetch", "origin", commit)
    return repository, str(directory.resolve())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="data")
    parser.add_argument("--cohort", choices=["full", "lite"], default="lite")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--repos", default="repos")
    parser.add_argument("--out", default="configs/repo_map.json")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    data = Path(args.data)
    lite = set(json.loads((data / "lite_ids.json").read_text())) if args.cohort == "lite" else None
    instances = [r for r in read_jsonl(data / "instances.jsonl") if lite is None or r["instance_id"] in lite]
    if args.limit:
        instances = instances[:args.limit]
    grouped = defaultdict(set)
    for record in instances:
        grouped[record["repo"]].add(record["base_commit"])
    root = Path(args.repos).resolve()
    root.mkdir(parents=True, exist_ok=True)
    existing = json.loads(Path(args.out).read_text()) if Path(args.out).exists() else {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(prepare, repo, commits, root, not args.check_only)
                   for repo, commits in sorted(grouped.items())]
        for future in futures:
            repository, directory = future.result()
            existing[repository] = directory
            print("Ready: " + repository)
    write_json(args.out, existing)
    print(f"Prepared {len(grouped)} repositories for {len(instances)} task records")


if __name__ == "__main__":
    main()
