"""Delegate canonical task input to an official-agent bridge.

PTRACE_NATIVE_ARGV contains the bridge command as a JSON argument array.
The bridge reads task input from stdin and exports one run object to stdout."""
import argparse
import json
import os
import subprocess
import sys


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--agent", required=True, choices=["Agentless", "AutoCodeRover", "OpenHands", "SWE-agent", "Augment", "Codex", "Claude Code"])
    args = p.parse_args()
    payload = json.load(sys.stdin)
    value = os.environ.get("PTRACE_NATIVE_ARGV")
    if not value:
        raise ValueError("Configure PTRACE_NATIVE_ARGV for the installed official agent bridge")
    argv = json.loads(value)
    if not isinstance(argv, list) or not all(isinstance(x, str) for x in argv):
        raise ValueError("PTRACE_NATIVE_ARGV must be a JSON argument array")
    if not argv:
        raise ValueError("Empty backend command")
    process = subprocess.run(argv, input=json.dumps(payload), capture_output=True, text=True, shell=False, env=os.environ.copy())
    if process.returncode:
        raise RuntimeError("Official backend failed")
    result = json.loads(process.stdout)
    result["agent"] = args.agent
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(type(exc).__name__ + ": official backend integration failed", file=sys.stderr)
        sys.exit(1)
