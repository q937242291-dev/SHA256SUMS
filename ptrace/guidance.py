"""Generate-then-check, trap-aware, and multi-hop guidance contracts."""
import copy

from .io import public_instance, public_baseline
from .metrics import baseline_signature


from .prompts import load_prompt

MODE_INSTRUCTIONS = {mode: load_prompt("guide_" + mode) for mode in ("check", "trap", "multi")}


def guidance_input(instance, baseline, mode, repository_files):
    """Build an independent mode input from the same baseline state."""
    if mode not in MODE_INSTRUCTIONS:
        raise ValueError("Unknown guidance mode")
    if baseline.get("instance_id") != instance["instance_id"] or baseline.get("base_commit") != instance["base_commit"]:
        raise ValueError("Baseline issue/revision mismatch")
    return {"issue": public_instance(instance), "repository_files": list(repository_files),
            "baseline": copy.deepcopy(public_baseline(baseline)),
            "guidance": MODE_INSTRUCTIONS[mode], "guidance_mode": mode,
            "retrieval_contract": copy.deepcopy(baseline.get("retrieval_contract", {}))}


def attach_pair_identity(result, baseline):
    result["baseline_run_id"] = baseline["run_id"]
    result["baseline_input_signature"] = baseline_signature(baseline)
    return result


def transition(baseline_score, guided_score, k=5):
    """Describe acquisition, withdrawal, persistence, and regression separately."""
    support, fc, filehit = f"Support@{k}", f"FC@{k}", f"File@{k}"
    if baseline_score.get("status") != "completed" or guided_score.get("status") != "completed":
        return "unobserved"
    if any(row.get(key) is None for row in (baseline_score, guided_score) for key in (support, fc, filehit)):
        return "unobserved"
    if baseline_score[support] == 0 and guided_score[support] == 1:
        return "evidence-acquisition"
    if baseline_score[support] == 1 and guided_score[support] == 0:
        return "support-regression"
    if baseline_score[fc] == 1 and guided_score[filehit] == 0:
        return "unsupported-hit-withdrawal"
    if baseline_score[fc] == 1 and guided_score[fc] == 1:
        return "persistent-false-credit"
    if baseline_score[fc] == 0 and guided_score[fc] == 1:
        return "false-credit-regression"
    return "unchanged"
