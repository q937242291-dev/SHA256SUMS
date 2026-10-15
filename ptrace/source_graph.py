"""Revision-specific Python artifacts and statically resolved source bindings."""
import ast
import re
from collections import defaultdict, deque
from pathlib import PurePosixPath

from .audit import edge_key
from .io import digest


FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _walk_scope(node):
    """Walk one executable scope, excluding nested function and class bodies."""
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, FUNCTIONS + (ast.ClassDef, ast.Lambda)):
            continue
        yield from _walk_scope(child)


def _module_names(file):
    parts = list(PurePosixPath(file).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    names = {".".join(parts)}
    if parts and parts[0] in ("src", "lib", "python"):
        names.add(".".join(parts[1:]))
    return sorted(n for n in names if n) or ["$root"]


def _qualified_name(expr):
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        base = _qualified_name(expr.value)
        return base + "." + expr.attr if base else None
    return None


class SourceGraph:
    """Index declarations, imports, and direct calls in one immutable snapshot."""

    def __init__(self, snapshot, files=None):
        self.snapshot = snapshot
        self.nodes, self.edges = {}, []
        self.modules, self.module_files = {}, defaultdict(list)
        self.functions, self.classes = {}, {}
        self.functions_by_file = defaultdict(list)
        self.imports, self.global_bindings = {}, {}
        self.adjacency = defaultdict(list)
        self._edge_index = defaultdict(list)
        for file in sorted(files if files is not None else snapshot.files()):
            if not file.endswith(".py"):
                continue
            try:
                tree = ast.parse(snapshot.text(file))
            except (SyntaxError, ValueError):
                continue
            self.modules[file] = tree
            for name in _module_names(file):
                self.module_files[name].append(file)
            self._index_declarations(file, tree)
        for file, tree in self.modules.items():
            self._index_imports(file, tree)
        for file, tree in self.modules.items():
            self._index_calls(file, tree)

    def _artifact(self, file, symbol, node):
        nid = file + "::" + symbol
        end = getattr(node, "end_lineno", node.lineno)
        declaration_end = min(end, node.lineno + 1)
        self.nodes[nid] = {
            "node_id": nid, "kind": "artifact", "file": file, "symbol": symbol,
            "start_line": node.lineno, "end_line": end,
            "anchors": [self.snapshot.anchor(file, node.lineno, declaration_end)],
        }
        return nid

    def _index_declarations(self, file, tree):
        module_id = file + "::$module"
        lines = self.snapshot.text(file).splitlines()
        self.nodes[module_id] = {
            "node_id": module_id, "kind": "artifact", "file": file,
            "symbol": "$module", "start_line": 1, "end_line": len(lines),
            "anchors": [self.snapshot.anchor(file, 1, 1)] if lines else [],
        }
        counts = defaultdict(int)
        bindings = set()
        for node in tree.body:
            if isinstance(node, FUNCTIONS + (ast.ClassDef,)):
                counts[node.name] += 1
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                bindings |= {n.id for n in ast.walk(node)
                             if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        self.global_bindings[file] = bindings
        for node in tree.body:
            if isinstance(node, FUNCTIONS) and counts[node.name] == 1 and node.name not in bindings:
                nid = self._artifact(file, node.name, node)
                self.functions[nid] = (node, None)
                self.functions_by_file[file].append(nid)
            elif isinstance(node, ast.ClassDef) and counts[node.name] == 1 and node.name not in bindings:
                cid = self._artifact(file, node.name, node)
                self.classes[cid] = node
                method_counts = defaultdict(int)
                for child in node.body:
                    if isinstance(child, FUNCTIONS):
                        method_counts[child.name] += 1
                for child in node.body:
                    if isinstance(child, FUNCTIONS) and method_counts[child.name] == 1:
                        nid = self._artifact(file, node.name + "." + child.name, child)
                        self.functions[nid] = (child, cid)
                        self.functions_by_file[file].append(nid)

    def _module_file(self, module):
        files = self.module_files.get(module, [])
        return files[0] if len(files) == 1 else None

    def _index_imports(self, file, tree):
        names = {}
        duplicates = set()
        base = min(_module_names(file), key=lambda n: (n.count("."), len(n))).split(".")
        if not file.endswith("/__init__.py"):
            base = base[:-1]
        for node in tree.body:
            values = []
            if isinstance(node, ast.Import):
                for alias in node.names:
                    binding = alias.asname or alias.name.split(".")[0]
                    module = alias.name if alias.asname else binding
                    values.append((binding, module, None))
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    keep = len(base) - node.level + 1
                    if keep < 0:
                        continue
                    module = ".".join(base[:keep] + ([node.module] if node.module else []))
                else:
                    module = node.module or ""
                for alias in node.names:
                    if alias.name != "*":
                        values.append((alias.asname or alias.name, module, alias.name))
            for binding, module, symbol in values:
                if binding in names:
                    duplicates.add(binding)
                names[binding] = (module, symbol, node)
                target_file = self._module_file(module)
                if target_file and self.nodes[target_file + "::$module"]["anchors"]:
                    self._edge(file + "::$module", target_file + "::$module", "imports",
                               [self.snapshot.anchor(file, node.lineno, node.end_lineno),
                                *self.nodes[target_file + "::$module"]["anchors"]])
        self.imports[file] = {k: v for k, v in names.items()
                              if k not in duplicates and k not in self.global_bindings[file]}

    def _resolve_call(self, file, caller_id, function, expr, class_id, shadowed):
        name = _qualified_name(expr)
        if not name:
            return None, []
        parts = name.split(".")
        root = parts[0]
        if class_id and root in ("self", "cls") and len(parts) == 2:
            cls = self.classes[class_id]
            if cls.bases or any(isinstance(n, FUNCTIONS) and n.name in ("__getattr__", "__getattribute__") for n in cls.body):
                return None, []
            for node in ast.walk(cls):
                if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)) and node.attr == parts[1]:
                    return None, []
                if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                    if node.id == root:
                        return None, []
            target = class_id + "." + parts[1]
            candidate = self.functions.get(target)
            if candidate and not candidate[0].decorator_list:
                return target, []
            return None, []
        if root in shadowed:
            return None, []
        if root in self.imports[file]:
            module, imported, statement = self.imports[file][root]
            suffix = ([imported] if imported else []) + parts[1:]
            if not suffix:
                return None, []
            for split in range(len(suffix), 0, -1):
                module_name = ".".join([module] + suffix[:split-1])
                target_file = self._module_file(module_name)
                if target_file:
                    target = target_file + "::" + ".".join(suffix[split-1:])
                    if target in self.functions and not self.functions[target][0].decorator_list:
                        return target, [self.snapshot.anchor(file, statement.lineno, statement.end_lineno)]
            return None, []
        target = file + "::" + name
        if target in self.functions and not self.functions[target][0].decorator_list:
            if len(parts) == 1 or (file + "::" + root in self.classes and not self.classes[file + "::" + root].bases):
                return target, []
        return None, []

    def _index_calls(self, file, tree):
        for caller_id in self.functions_by_file[file]:
            function, class_id = self.functions[caller_id]
            arguments = function.args.posonlyargs + function.args.args + function.args.kwonlyargs
            shadowed = {a.arg for a in arguments}
            for argument in (function.args.vararg, function.args.kwarg):
                if argument:
                    shadowed.add(argument.arg)
            for node in _walk_scope(function):
                if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                    shadowed.add(node.id)
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    shadowed |= {a.asname or a.name.split(".")[0] for a in node.names}
            for node in _walk_scope(function):
                if not isinstance(node, ast.Call):
                    continue
                target, extra = self._resolve_call(file, caller_id, function, node.func, class_id, shadowed)
                if target is None or target == caller_id:
                    continue
                relation = "test_calls" if function.name.startswith("test_") or "/tests/" in "/" + file else "calls"
                anchors = [self.snapshot.anchor(file, node.lineno, node.end_lineno),
                           *extra, *self.nodes[target]["anchors"]]
                self._edge(caller_id, target, relation, anchors)

    def _edge(self, source, destination, relation, anchors):
        edge = {"edge_id": "edge-" + digest([source, relation, destination, anchors])[:16],
                "src": source, "dst": destination, "relation": relation, "anchors": anchors}
        self.edges.append(edge)
        self.adjacency[source].append(edge)
        route = {"nodes": [self.nodes[source], self.nodes[destination]]}
        self._edge_index[edge_key(route, edge)].append(edge)

    def relation(self, source, destination, relation):
        matches = self.relation_candidates(source, destination, relation)
        return matches[0] if matches else None

    def relation_candidates(self, source, destination, relation):
        route = {"nodes": [{**source, "node_id": "s"}, {**destination, "node_id": "d"}]}
        key = edge_key(route, {"src": "s", "dst": "d", "relation": relation})
        return self._edge_index.get(key, [])

    def issue_edges(self, instance):
        text = instance["problem_statement"]
        edges = []
        for nid, node in sorted(self.nodes.items()):
            symbol = node["symbol"]
            needle = node["file"] if symbol == "$module" else symbol
            pattern = r"(?<![A-Za-z0-9_])" + re.escape(needle) + r"(?![A-Za-z0-9_])"
            match = re.search(pattern, text)
            if match is None or not node["anchors"]:
                continue
            issue_anchor = {"kind": "issue", "start_char": match.start(), "end_char": match.end(),
                            "sha256": digest_string(match.group())}
            edges.append({"edge_id": "mention-" + digest([instance["instance_id"], nid])[:16],
                          "src": "issue", "dst": nid, "relation": "mentions",
                          "anchors": [issue_anchor, *node["anchors"]]})
        return edges

    def paths(self, instance, endpoints=None, max_hops=12, limit=100, edge_filter=None):
        """Enumerate complete alternatives with bounded breadth-first traversal."""
        issue = {"node_id": "issue", "kind": "issue"}
        mentions = [e for e in self.issue_edges(instance) if edge_filter is None or edge_filter(e)]
        queue = deque((["issue", e["dst"]], [e]) for e in mentions)
        result = []
        expansions = 0
        while queue and len(result) < limit and expansions < 100000:
            node_ids, edges = queue.popleft()
            expansions += 1
            last = self.nodes[node_ids[-1]]
            if endpoints is None or last["file"] in endpoints:
                result.append({"route_id": "path-" + digest([node_ids, [e["edge_id"] for e in edges]])[:16],
                               "nodes": [issue] + [dict(self.nodes[n]) for n in node_ids[1:]],
                               "edges": [dict(e) for e in edges]})
            if len(edges) >= max_hops:
                continue
            for edge in sorted(self.adjacency[node_ids[-1]], key=lambda e: (e["dst"], e["relation"])):
                if edge["dst"] not in node_ids and (edge_filter is None or edge_filter(edge)):
                    queue.append((node_ids + [edge["dst"]], edges + [edge]))
        return result

    def export(self):
        return {"base_commit": self.snapshot.revision,
                "nodes": list(self.nodes.values()), "edges": self.edges}


def digest_string(text):
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()
