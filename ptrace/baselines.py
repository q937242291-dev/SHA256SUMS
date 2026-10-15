"""Context baselines and source-exploration reference workflows.

Official-agent integrations are provided by adapters.py."""
import ast
import collections
import math
import re
import time
import copy
from .io import public_instance, public_observations, unique_files, digest
from .repository import get_snapshot
from .metrics import baseline_signature
from .privacy import sanitize

from .prompts import load_prompt

SYSTEM = load_prompt("agent_system")

from .guidance import MODE_INSTRUCTIONS as GUIDANCE, guidance_input


def tokenize(text):
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return re.findall(r"[a-z0-9]+", text.lower())


def bm25(query, documents, k1=1.5, b=0.75):
    """Robertson-style BM25 ranking over source text and repository paths."""
    q = set(tokenize(query))
    bags = [collections.Counter(tokenize(name + "\n" + txt)) for name, txt in documents]
    lengths = [sum(x.values()) for x in bags]
    avg = sum(lengths) / len(lengths) if lengths else 1
    df = {word: sum(word in bag for bag in bags) for word in q}
    out = []
    for (name, txt), bag, length in zip(documents, bags, lengths):
        score = 0.0
        for word in q:
            tf = bag.get(word, 0)
            idf = math.log(1 + (len(bags) - df[word] + .5) / (df[word] + .5))
            score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / (avg or 1))) if tf else 0
        out.append((name, score))
    return sorted(out, key=lambda x: (-x[1], x[0]))


class Tools:
    def __init__(self, instance, snapshot, observations=None):
        self.instance = instance
        self.snapshot = snapshot
        self.files = snapshot.files()
        self.observations = copy.deepcopy(observations or [])
        self._graph = None

    def _append(self, action, spans, extra=None):
        step = max((o.get("step", 0) for o in self.observations), default=0) + 1
        obj = {"step": step, "base_commit": self.instance["base_commit"], "action": action, "spans": spans}
        if extra:
            obj.update(extra)
        self.observations.append(obj)
        return obj

    def read(self, filename, start=1, end=None):
        lines = self.snapshot.text(filename).splitlines()
        if not lines:
            return self._append({"type": "read", "file": filename}, [], {"empty_file": True})
        end = min(end or min(len(lines), start + 199), len(lines))
        text = self.snapshot.span(filename, start, end)
        return self._append({"type": "read", "file": filename, "start_line": start, "end_line": end},
                            [{"file": filename, "start_line": start, "end_line": end, "text": text}])

    def search(self, query, limit=20):
        # Match literal source text for the requested query.
        if not isinstance(query, str) or not query:
            raise ValueError("Search query must be nonempty")
        spans = []
        for file in self.files:
            try:
                lines = self.snapshot.text(file).splitlines()
            except ValueError:
                continue
            for line, text in enumerate(lines, 1):
                if query in text:
                    a, z = max(1, line - 2), min(len(lines), line + 2)
                    spans.append({"file": file, "start_line": a, "end_line": z, "text": "\n".join(lines[a-1:z])})
                    if len(spans) >= limit:
                        return self._append({"type": "search", "query": query}, spans)
        return self._append({"type": "search", "query": query}, spans)

    def symbols(self, file):
        text = self.snapshot.text(file)
        if file.endswith(".py"):
            try:
                tree = ast.parse(text)
                spans = [{"file": file, "start_line": n.lineno, "end_line": getattr(n, "end_lineno", n.lineno),
                          "text": self.snapshot.span(file, n.lineno, getattr(n, "end_lineno", n.lineno))}
                         for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
            except SyntaxError:
                spans = []
        else:
            patterns = {
                ".go": r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)",
                ".js": r"\b(?:function|class)\s+([A-Za-z_$][\w$]*)",
                ".ts": r"\b(?:function|class|interface|enum)\s+([A-Za-z_$][\w$]*)",
            }
            pattern = patterns.get("." + file.rsplit(".", 1)[-1])
            spans = []
            if pattern:
                lines = text.splitlines()
                for i, line in enumerate(lines, 1):
                    match = re.search(pattern, line)
                    if match:
                        end = min(i + 3, len(lines))
                        spans.append({"file": file, "start_line": i, "end_line": end,
                                      "text": self.snapshot.span(file, i, end), "symbol": match.group(1)})
        return self._append({"type": "symbols", "file": file}, spans[:20])

    def relations(self, file, symbol, limit=20):
        """Return source spans for resolved calls and imports of an artifact."""
        from .artifact_graph import ArtifactGraph as SourceGraph
        if self._graph is None:
            self._graph = SourceGraph(self.snapshot)
        nid = file + "::" + (symbol or "$module")
        edges = self._graph.adjacency.get(nid, [])[:limit]
        spans, seen = [], set()
        for edge in edges:
            for anchor in edge["anchors"]:
                key = (anchor["file"], anchor["start_line"], anchor["end_line"])
                if key not in seen:
                    seen.add(key)
                    spans.append({"file": key[0], "start_line": key[1], "end_line": key[2],
                                  "text": self.snapshot.span(*key)})
        return self._append({"type": "relations", "file": file, "symbol": symbol}, spans,
                            {"relations": [{k: v for k, v in e.items() if k != "anchors"} for e in edges]})


def _documents(snapshot, limit=2_000_000):
    docs = []
    for file in snapshot.files():
        try:
            text = snapshot.text(file)
        except ValueError:
            continue
        if len(text) <= limit:
            docs.append((file, text))
    return docs


def _answers(obj, allowed):
    predicted = unique_files(obj.get("predicted_files", []))
    if not set(predicted) <= set(allowed):
        raise ValueError("Predictions must exist in the pre-fix repository")
    routes = obj.get("routes", [])
    if not isinstance(routes, list):
        raise ValueError("routes must be a list")
    if len({r["route_id"] for r in routes}) != len(routes):
        raise ValueError("Duplicate route IDs")
    result = {"predicted_files": predicted, "routes": routes, "selected_route_id": obj.get("selected_route_id")}
    if obj.get("acceptance") in ("retain", "revise", "withdraw"):
        result["acceptance"] = obj["acceptance"]
    return result


def run_baseline(instance, repo_map, method, llm=None, max_steps=15, max_chars=60000, truth=None, baseline=None, guidance=None):
    instance = public_instance(instance)
    retrieval_contract = copy.deepcopy(baseline.get("retrieval_contract")) if baseline else None
    if retrieval_contract:
        if retrieval_contract.get("base_commit") != instance["base_commit"]:
            raise ValueError("Retrieval contract revision mismatch")
        max_steps = retrieval_contract["max_steps"]
    else:
        retrieval_contract = {"base_commit": instance["base_commit"],
                              "allowed_actions": ["search", "read", "symbols", "relations", "finish"],
                              "max_steps": max_steps, "search_limit": 20}
    snapshot = get_snapshot(instance, repo_map)
    started = time.monotonic()
    tokens_before = llm.tokens if llm else 0
    tools = Tools(instance, snapshot, baseline.get("observations") if baseline else None)
    access = baseline.get("access_contract") if baseline else "pre_fix_source_only_v1"
    protocol = baseline.get("protocol") if baseline else "native"
    agent = baseline.get("agent") if baseline else method
    out = {"run_id": "run-" + digest([instance["instance_id"], agent, llm.label if llm else "none", guidance or "baseline", method])[:20],
           "instance_id": instance["instance_id"], "base_commit": instance["base_commit"], "agent": agent,
           "model": llm.label if llm else "none", "actual_model": llm.model if llm else None,
           "condition": guidance or "baseline", "protocol": protocol, "access_contract": access, "status": "completed", "weight": 1}
    out["retrieval_contract"] = retrieval_contract
    if baseline:
        out["baseline_run_id"] = baseline["run_id"]
        out["baseline_input_signature"] = baseline_signature(baseline)
    context = {"issue": instance, "repository_files": tools.files}
    context["retrieval_contract"] = retrieval_contract
    if baseline and guidance:
        context = guidance_input(instance, baseline, guidance, tools.files)
    if baseline and llm and (baseline.get("model") != llm.label or baseline.get("actual_model") != llm.model):
        raise ValueError("Guidance requires the baseline model and actual deployment")
    try:
        if method == "bm25":
            ranked = bm25(instance["problem_statement"], _documents(snapshot))
            # Retrieve candidate files from the source-text index.
            out.update({"predicted_files": [x[0] for x in ranked[:10]], "routes": [], "selected_route_id": None})
        elif method in ("bm25-rerank", "long-context", "oracle-chain"):
            if llm is None:
                raise ValueError("This baseline requires a configured model")
            docs = _documents(snapshot)
            if method == "bm25-rerank":
                rank = bm25(instance["problem_statement"], docs)
                names = [x[0] for x in rank[:30]]
            elif method == "oracle-chain":
                if not truth or truth.get("graph_source_verified") is not True:
                    raise ValueError("Chain-grounded oracle requires independently verified reference paths")
                names = sorted({n["file"] for r in truth["verified_reference_paths"] for n in r["nodes"] if n.get("file")})
                context["supplied_verified_paths"] = truth["verified_reference_paths"]
                context["oracle_targets"] = truth["target_files"]
                out["protocol"] = "supplied_verified_context_oracle"
                out["access_contract"] = "explicit_oracle_labels_v1"
            else:
                names = [x[0] for x in docs]
            remaining = max_chars
            for name in names:
                lines = snapshot.text(name).splitlines()
                kept, count = [], 0
                for line in lines:
                    if count + len(line) + 1 > remaining:
                        break
                    kept.append(line); count += len(line) + 1
                if kept:
                    tools.read(name, 1, len(kept)); remaining -= count
                if remaining <= 0:
                    break
            context["observations"] = public_observations(tools.observations)
            system = load_prompt("oracle_system") if method == "oracle-chain" else SYSTEM
            out.update(_answers(llm.json(system, json_text(context)), tools.files))
        elif method in ("reference-staged", "reference-structured", "reference-native", "ptrace") or baseline:
            if llm is None:
                raise ValueError("Reference exploration requires a configured model")
            if method == "reference-staged" and not baseline:
                from .localization import staged_localization
                context["localization"] = staged_localization(instance, tools, llm, max_chars=max_chars)
            elif method == "reference-structured" and not baseline:
                from .localization import structured_localization
                context["localization"] = structured_localization(instance, tools, llm)
            for step in range(max_steps):
                context["observations"] = public_observations(tools.observations)
                request = llm.json(SYSTEM + "\n" + load_prompt("source_tools"), json_text(context))
                action = request.get("action")
                if action not in retrieval_contract["allowed_actions"]:
                    raise ValueError("Action is outside the recorded retrieval contract")
                if action == "finish":
                    out.update(_answers(request, tools.files)); break
                if action == "search":
                    tools.search(request["query"], retrieval_contract["search_limit"])
                elif action == "read":
                    tools.read(request["file"], int(request.get("start_line", 1)), int(request.get("end_line", request.get("start_line", 1) + 199)))
                elif action == "symbols":
                    tools.symbols(request["file"])
                elif action == "relations":
                    tools.relations(request["file"], request.get("symbol"))
                else:
                    raise ValueError("Unknown exploration action")
            if "predicted_files" not in out:
                out["status"] = "incomplete"
                out.update({"predicted_files": None, "routes": None})
        else:
            raise ValueError("Unknown baseline method")
    except Exception as exc:
        if isinstance(exc, ValueError) and ("requires" in str(exc) or "Unknown baseline" in str(exc)):
            raise
        out.update({"status": "failed", "error_class": type(exc).__name__, "predicted_files": None, "routes": None})
    out["observations"] = tools.observations
    files_seen = {s["file"] for o in tools.observations for s in o.get("spans", [])}
    out["resources"] = {"tokens": llm.tokens - tokens_before if llm and llm.tokens_observed else (0 if not llm else None),
                        "files": len(files_seen), "time_s": time.monotonic() - started, "file_count_definition": "distinct_visible_span_files"}
    if baseline:
        prior = baseline.get("resources", {})
        for key in ("tokens", "time_s"):
            out["resources"][key] = prior.get(key) + out["resources"][key] if prior.get(key) is not None and out["resources"][key] is not None else None
    return sanitize(out)


def json_text(obj):
    import json
    return json.dumps(obj, ensure_ascii=False)
