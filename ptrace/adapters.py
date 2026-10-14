"""Official-agent process integration and native trajectory conversion."""
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from .io import public_instance, public_baseline, digest, unique_files
from .privacy import sanitize

AGENTS = ("Agentless", "AutoCodeRover", "OpenHands", "SWE-agent", "Augment", "Codex", "Claude Code")


def external_run(instance, config, model_label, condition="baseline", baseline=None):
    if config.get("agent") not in AGENTS:
        raise ValueError("Unsupported official agent identity")
    if not config.get("revision") or config["revision"].startswith("SET_") or not config.get("command"):
        raise ValueError("Pin the actual installed agent revision and JSON wrapper command")
    argv = config["command"]
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        raise ValueError("Adapter command must be an argument array")
    payload = {"instance": public_instance(instance), "condition": condition, "model_label": model_label}
    if baseline:
        payload["baseline"] = public_baseline(baseline)
        payload["protocol"] = baseline.get("protocol")
        payload["access_contract"] = baseline.get("access_contract")
        payload["baseline_resources"] = baseline.get("resources", {})
        payload["retrieval_contract"] = baseline.get("retrieval_contract", {})
        from .guidance import MODE_INSTRUCTIONS
        payload["guidance"] = MODE_INSTRUCTIONS[condition]
    started = time.monotonic()
    process = subprocess.run(argv, input=json.dumps(payload), text=True, capture_output=True,
                             timeout=config.get("timeout_s", 3600), cwd=config.get("working_directory"), env=os.environ.copy(), shell=False)
    if process.returncode:
        raise RuntimeError("Official agent wrapper returned a nonzero exit status")
    try:
        result = json.loads(process.stdout)
    except ValueError:
        raise ValueError("Official wrapper must return one JSON object on stdout") from None
    if result.get("instance_id") != instance["instance_id"] or result.get("base_commit") != instance["base_commit"]:
        raise ValueError("Official wrapper issue/revision mismatch")
    result["agent"] = config["agent"]
    result["agent_revision"] = config["revision"]
    result["model"] = model_label
    result["condition"] = condition
    result.setdefault("run_id", "native-" + digest([instance["instance_id"], config["agent"], model_label, condition])[:20])
    result.setdefault("protocol", "native")
    result.setdefault("access_contract", "pre_fix_source_only_v1")
    result.setdefault("resources", {})
    result["resources"].setdefault("time_s", time.monotonic() - started)
    result["weight"] = 1
    if result.get("predicted_files") is not None:
        result["predicted_files"] = unique_files(result["predicted_files"])
    if baseline:
        from .metrics import baseline_signature
        result["baseline_run_id"] = baseline["run_id"]
        result["baseline_input_signature"] = baseline_signature(baseline)
        if baseline.get("retrieval_contract"):
            if result.get("retrieval_contract") not in (None, baseline["retrieval_contract"]):
                raise ValueError("Official guidance changed the recorded retrieval contract")
            result["retrieval_contract"] = baseline["retrieval_contract"]
    # Native exports preserve observation availability.
    if "observations" not in result:
        result["observations"] = None
    if "routes" not in result:
        result["routes"] = None
    return sanitize(result)


def import_native(path, instance, agent, model_label, revision, format_name="canonical"):
    """Convert native exports into the canonical run schema."""
    obj = json.loads(Path(path).read_text(encoding="utf-8"))
    if format_name == "canonical":
        result = obj
    elif format_name == "sweagent":
        # Convert native tool actions and observations into ordered records.
        history = obj.get("trajectory", [])
        observations = [{"step": i, "base_commit": instance["base_commit"], "action": {"type": "shell", "command": row.get("action", "")},
                         "raw_text": row.get("observation", ""), "spans": []} for i, row in enumerate(history, 1)]
        result = {"status": "completed" if obj.get("info", {}).get("exit_status", "").startswith("submitted") else "incomplete",
                  "observations": observations, "predicted_files": obj.get("predicted_files"), "routes": obj.get("routes"),
                  "resources": {"tokens": obj.get("info", {}).get("model_stats", {}).get("tokens_sent", 0) + obj.get("info", {}).get("model_stats", {}).get("tokens_received", 0)
                                if obj.get("info", {}).get("model_stats") else None}}
    elif format_name == "agentless":
        result = {"status": "completed", "predicted_files": obj.get("found_files"), "routes": obj.get("routes"),
                  "observations": obj.get("observations"), "resources": obj.get("resources", {})}
    else:
        raise ValueError("Use the canonical export format for this runtime")
    result.update({"instance_id": instance["instance_id"], "base_commit": instance["base_commit"], "agent": agent,
                   "model": model_label, "agent_revision": revision, "condition": "baseline", "protocol": "native",
                   "access_contract": "pre_fix_source_only_v1", "weight": 1})
    result.setdefault("run_id", "import-" + digest([instance["instance_id"], agent, model_label, revision])[:20])
    return sanitize(result)
