"""JSONL interfaces, public input projection, and repository path utilities."""
import hashlib
import json
import math
from pathlib import Path, PurePosixPath


def read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at line {lineno}") from exc
            if not isinstance(obj, dict):
                raise ValueError(f"JSONL line {lineno} must be an object")
            yield obj


def write_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n")


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def digest(obj):
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def file_digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def index_unique(rows, key="instance_id"):
    out = {}
    for r in rows:
        v = r[key]
        if v in out:
            raise ValueError(f"Duplicate {key}: {v}")
        out[v] = r
    return out


def repo_path(value):
    """Normalize a repository-relative path while preserving case sensitivity."""
    if not isinstance(value, str):
        raise ValueError("File path must be a string")
    value = value.replace("\\", "/")
    p = PurePosixPath(value)
    if not value or p.is_absolute() or ".." in p.parts or ":" in value or ".git" in p.parts:
        raise ValueError("Expected a relative repository path")
    return str(p)


def unique_files(values):
    out = []
    for value in values:
        p = repo_path(value)
        if p not in out:
            out.append(p)
    return out


def public_instance(r):
    """Return the agent-facing input fields from a task record."""
    return {k: r[k] for k in ("instance_id", "repo", "base_commit", "problem_statement", "language") if k in r}


def public_route(route):
    """Project a route proposal into the agent-visible artifact interface."""
    return {"route_id": route["route_id"],
            "nodes": [{k: n[k] for k in ("node_id", "kind", "file", "symbol", "value", "evidence") if k in n}
                      for n in route.get("nodes", [])],
            "edges": [{k: e[k] for k in ("edge_id", "src", "dst", "relation", "evidence") if k in e}
                      for e in route.get("edges", [])]}


def public_observations(observations):
    """Project recorded actions and returned source spans for prompt input."""
    if observations is None:
        return None
    rows = []
    for observation in observations:
        row = {k: observation[k] for k in ("step", "base_commit", "action", "raw_text") if k in observation}
        if "relations" in observation:
            row["relations"] = [{k: r[k] for k in ("edge_id", "src", "dst", "relation") if k in r}
                                for r in observation["relations"]]
        row["spans"] = [{k: s[k] for k in ("file", "start_line", "end_line", "text", "symbol") if k in s}
                        for s in observation.get("spans", [])]
        rows.append(row)
    return rows


def public_baseline(baseline):
    return {"predicted_files": baseline.get("predicted_files"),
            "routes": [public_route(r) for r in baseline.get("routes") or []]
            if baseline.get("routes") is not None else None,
            "selected_route_id": baseline.get("selected_route_id"),
            "observations": public_observations(baseline.get("observations"))}


def finite_weight(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("Weights must be finite and nonnegative")
    return float(value)
