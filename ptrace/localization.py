"""File, symbol, and line localization with explicit source observations."""
import json

from .io import unique_files
from .prompts import load_prompt


def _request(llm, instruction, payload):
    return llm.json(instruction, json.dumps(payload, ensure_ascii=False))


def staged_localization(instance, tools, llm, max_files=10, max_chars=60000):
    """Execute file selection, symbol inspection, and line-range localization."""
    files = _request(
        llm,
        load_prompt("staged_files"),
        {"issue": instance, "repository_files": tools.files})
    selected = [f for f in unique_files(files.get("predicted_files", [])) if f in tools.files][:max_files]
    for file in selected:
        tools.symbols(file)
    symbols = _request(
        llm,
        load_prompt("staged_symbols"),
        {"issue": instance, "file_shortlist": selected, "observations": tools.observations})
    remaining = max_chars
    locations = []
    for location in symbols.get("locations", []):
        file = location.get("file")
        if file not in selected:
            continue
        start = int(location.get("start_line", 1))
        end = min(int(location.get("end_line", start + 199)), start + 399)
        lines = tools.snapshot.text(file).splitlines()
        end = min(end, len(lines))
        while end >= start and len("\n".join(lines[start-1:end])) > remaining:
            end -= 1
        if end < start:
            continue
        observation = tools.read(file, start, end)
        remaining -= sum(len(span["text"]) for span in observation["spans"])
        locations.append({"file": file, "symbol": location.get("symbol"), "start_line": start, "end_line": end})
        if remaining <= 0:
            break
    return {"file_shortlist": selected, "localized_ranges": locations}


def structured_localization(instance, tools, llm, max_files=5):
    """Rank declarations and expose their resolved source-relation neighborhood."""
    from .baselines import bm25
    from .source_graph import SourceGraph
    graph = SourceGraph(tools.snapshot)
    tools._graph = graph
    names = {}
    for node in graph.nodes.values():
        if node["symbol"] != "$module":
            names.setdefault(node["file"], []).append(node["symbol"])
    ranked = bm25(instance["problem_statement"], [(file, " ".join(symbols)) for file, symbols in names.items()])
    selected = [file for file, score in ranked[:max_files] if score > 0]
    for file in selected:
        tools.symbols(file)
    chosen = _request(
        llm,
        load_prompt("structured_search"),
        {"issue": instance, "observations": tools.observations, "repository_files": tools.files})
    for artifact in chosen.get("artifacts", [])[:10]:
        if artifact.get("file") in tools.files:
            tools.relations(artifact["file"], artifact.get("symbol"))
    return {"structure_shortlist": selected}
