"""Artifact bindings in Python, JavaScript, TypeScript, and Go snapshots."""
import ast
import posixpath
import re
from collections import defaultdict
from pathlib import PurePosixPath

from .io import digest
from .source_graph import SourceGraph, _module_names, _qualified_name, _walk_scope, digest_string

COMMON_CLUES = frozenset('''a an the and or as at by for from in into of on to with without
name names value values default defaults true false null none option options config configuration
path paths file files type types data result results output input args kwargs self cls
enabled disabled enable disable title body content text message error errors test tests
version versions description current expected behavior example examples first last next previous
read write load save get set run use used using update create delete add remove return returns
main init start stop status state key keys item items field fields argument arguments
function functions method methods class classes module modules object objects string strings
number numbers list lists dict dictionary include includes settings setting model models
user users cache count length size format mode public private static dynamic
'''.split())


def precise_issue_clue(text, node, start, end):
    """Recognize API spellings and diagnostic literals used as issue anchors."""
    value=text[start:end]
    kind=node.get('symbol_kind')
    if kind=='module' or kind=='option':
        return False
    if kind=='option_name':
        before,after=text[max(0,start-1):start],text[end:end+1]
        explicit=(before in ('`','"',"'") or after in ('`','"',"'")
                  or text[:start].count('```')%2==1 or re.search(r'[._:-]|[a-z][A-Z]',value)
                  or (value.isupper() and len(value)>=4))
        return len(value)>=4 and value.lower() not in COMMON_CLUES and bool(explicit)
    if kind=='literal':
        words=re.findall(r'[A-Za-z0-9_]+',value)
        return len(value)>=12 and len(words)>=2 and any(w.lower() not in COMMON_CLUES for w in words)
    if value.lower() in COMMON_CLUES:
        return False
    before,after=text[max(0,start-2):start],text[end:end+2]
    code_spelling=(after.startswith(('(', '[', '.')) or before.endswith(('.', '`'))
                   or ('.' in value) or '_' in value or re.search(r'[a-z][A-Z]',value))
    return bool(code_spelling or (len(value)>=5 and value.lower() not in COMMON_CLUES))


class ArtifactGraph(SourceGraph):
    """Extend direct Python calls with declarations, values, and language bindings."""

    def __init__(self, snapshot, files=None):
        selected = sorted(files if files is not None else snapshot.files())
        super().__init__(snapshot, selected)
        self.declarations = defaultdict(dict)
        self.literal_values = defaultdict(list)
        self.class_bases = {}
        self.unsafe_calls = set()
        self._relations = {(e['src'], e['relation'], e['dst']) for e in self.edges}
        self._python_declarations()
        self._python_bindings()
        self._other_languages(selected)
        self._configuration(selected)

    def _bind(self, source, destination, relation, anchors):
        key = (source, relation, destination)
        if source == destination or key in self._relations or not anchors:
            return
        self._relations.add(key)
        self._edge(source, destination, relation, anchors)

    def _declare(self, file, symbol, start, end, kind, parent=None):
        nid = file + '::' + symbol
        if nid not in self.nodes:
            self.nodes[nid] = {
                'node_id': nid, 'kind': 'artifact', 'file': file, 'symbol': symbol,
                'start_line': start, 'end_line': end,
                'anchors': [self.snapshot.anchor(file, start, min(end, start + 1))],
            }
        self.nodes[nid]['symbol_kind'] = kind
        self.declarations[file][symbol] = nid
        if parent:
            self._bind(parent, nid, 'defines_method', self.nodes[nid]['anchors'])
        elif file + '::$module' in self.nodes:
            self._bind(file + '::$module', nid, 'declares', self.nodes[nid]['anchors'])
        return nid

    def _value(self, file, owner, value, start, end, kind='literal'):
        if not isinstance(value, str) or not 4 <= len(value) <= 300:
            return
        try:
            value.encode('utf-8')
        except UnicodeEncodeError:
            return
        nid = file + '::$value:' + digest([owner, value, start])[:12]
        if nid in self.nodes and self.nodes[nid].get('symbol_kind')=='option_name' and kind=='literal':
            return nid
        anchors = [self.snapshot.anchor(file, start, end)]
        self.nodes[nid] = {
            'node_id': nid, 'kind': 'artifact', 'file': file, 'symbol': nid.split('::', 1)[1],
            'symbol_kind': kind, 'value': value, 'start_line': start,
            'end_line': end, 'anchors': anchors,
        }
        self.literal_values[value].append(nid)
        if owner in self.nodes:
            self._bind(nid, owner, 'defined_in', anchors + self.nodes[owner]['anchors'])
        return nid

    def _option_value(self,file,owner,value,start,end,import_anchors=()):
        nid=self._value(file,owner,value,start,end,kind='option_name')
        if nid is None:
            return
        anchors=[self.snapshot.anchor(file,start,end),*import_anchors,*self.nodes[owner]['anchors']]
        self.nodes[nid]['anchors']=anchors
        for edge in self.adjacency[nid]:
            if edge['relation']=='defined_in':
                edge['anchors']=anchors
        self._bind(nid,owner,'configured_by',anchors)

    def _python_declarations(self):
        self.py_scopes = {}
        for file, tree in self.modules.items():
            module = file + '::$module'
            self.nodes[module]['symbol_kind'] = 'module'

            def visit(body, prefix='', parent=None):
                for node in body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        name = prefix + node.name
                        kind = 'class' if isinstance(node, ast.ClassDef) else 'method' if parent else 'function'
                        nid = self._declare(file, name, node.lineno, node.end_lineno, kind, parent)
                        if isinstance(node, ast.ClassDef):
                            self.class_bases[nid] = [(_qualified_name(b), b) for b in node.bases]
                            visit(node.body, name + '.', nid)
                        else:
                            self.py_scopes[nid] = node
                            if node.decorator_list:
                                self.unsafe_calls.add(nid)
                    elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                        value = node.value
                        for target in targets:
                            if isinstance(target, ast.Name):
                                nid = self._declare(file, prefix + target.id, node.lineno, node.end_lineno,
                                                    'constant' if target.id.isupper() else 'binding')
                                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                                    self._value(file, nid, value.value, value.lineno, value.end_lineno)
                                if isinstance(value,ast.Dict) and re.search(r'(?:config|settings|options)',file,re.I):
                                    for key in value.keys:
                                        if isinstance(key,ast.Constant) and isinstance(key.value,str):
                                            self._value(file,nid,key.value,key.lineno,key.end_lineno,kind='option_name')
            visit(tree.body)
            for nid, node in list(self.py_scopes.items()):
                if self.nodes[nid]['file'] != file:
                    continue
                for child in _walk_scope(node):
                    if isinstance(child, ast.Constant) and isinstance(child.value, str):
                        self._value(file, nid, child.value, child.lineno, child.end_lineno)

    def _resolve_python(self, file, name, seen=None):
        if not name:
            return None
        seen = set() if seen is None else seen
        if (file, name) in seen:
            return None
        seen.add((file, name))
        if name in self.declarations[file]:
            return self.declarations[file][name]
        parts = name.split('.')
        binding = self.imports[file].get(parts[0])
        if not binding:
            return None
        module, imported, _ = binding
        remaining = ([imported] if imported else []) + parts[1:]
        for split in range(len(remaining), -1, -1):
            target_file = self._module_file('.'.join([module] + remaining[:split]))
            if target_file:
                tail = '.'.join(remaining[split:])
                if tail:
                    resolved = self._resolve_python(target_file, tail, seen)
                    if resolved:
                        return resolved
        return None

    def _python_bindings(self):
        for cid, bases in self.class_bases.items():
            file = self.nodes[cid]['file']
            for name, expr in bases:
                dest = self._resolve_python(file, name)
                if dest and self.nodes[dest]['symbol_kind'] == 'class':
                    self._bind(cid, dest, 'inherits', [self.snapshot.anchor(file, expr.lineno, expr.end_lineno),
                                                     *self.nodes[dest]['anchors']])
        for caller, function in self.py_scopes.items():
            file = self.nodes[caller]['file']
            locals_ = {a.arg for a in function.args.posonlyargs + function.args.args + function.args.kwonlyargs}
            for argument in (function.args.vararg, function.args.kwarg):
                if argument:
                    locals_.add(argument.arg)
            local_types = {}
            counts = defaultdict(int)
            for item in _walk_scope(function):
                if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store):
                    counts[item.id] += 1
                    locals_.add(item.id)
                if isinstance(item, (ast.Import, ast.ImportFrom)):
                    locals_.update(a.asname or a.name.split('.')[0] for a in item.names)
            for item in _walk_scope(function):
                if isinstance(item, ast.Assign) and len(item.targets) == 1 and isinstance(item.targets[0], ast.Name):
                    variable = item.targets[0].id
                    if counts[variable] == 1 and isinstance(item.value, ast.Call):
                        target = self._resolve_python(file, _qualified_name(item.value.func))
                        if target and self.nodes[target]['symbol_kind'] == 'class':
                            local_types[variable] = (target, item)
            for item in _walk_scope(function):
                if isinstance(item, ast.Call):
                    name = _qualified_name(item.func)
                    if not name:
                        continue
                    root, *suffix = name.split('.')
                    extra = []
                    imported=self.imports[file].get(root)
                    if imported and root not in locals_:
                        module,member,statement=imported
                        spelling='.'.join(([member] if member else [])+suffix)
                        if module=='os' and spelling in ('getenv','environ.get') and item.args:
                            argument=item.args[0]
                            if isinstance(argument,ast.Constant) and isinstance(argument.value,str):
                                self._option_value(file,caller,argument.value,item.lineno,item.end_lineno,
                                                   [self.snapshot.anchor(file,statement.lineno,statement.end_lineno)])
                    if root in ('self', 'cls') and len(suffix) == 1 and self.nodes[caller]['symbol_kind'] == 'method':
                        cls = caller.rsplit('.', 1)[0]
                        dest = cls + '.' + suffix[0]
                        class_node = self.classes.get(cls)
                        mutated = class_node is None or any(
                            isinstance(n, ast.Attribute) and isinstance(n.ctx, (ast.Store, ast.Del)) and n.attr == suffix[0]
                            for n in ast.walk(class_node))
                        dynamic = any(cls + '.' + method in self.nodes for method in ('__getattr__', '__getattribute__'))
                        if dest not in self.py_scopes or mutated or dynamic:
                            dest = None
                        elif class_node:
                            extra.append(self.snapshot.anchor(file, class_node.lineno, class_node.lineno))
                    elif root in local_types and len(suffix) == 1:
                        cls, assignment = local_types[root]
                        dest = cls + '.' + suffix[0]
                        extra.append(self.snapshot.anchor(file, assignment.lineno, assignment.end_lineno))
                        if dest not in self.nodes:
                            dest = None
                    elif root not in locals_:
                        dest = self._resolve_python(file, name)
                    else:
                        dest = None
                    if dest and dest not in self.unsafe_calls and self.nodes[dest].get('symbol_kind') in ('function', 'method', 'class'):
                        # A class reference anchors its constructor without assuming dynamic dispatch.
                        relation = 'test_calls' if '/test' in '/' + file or function.name.startswith('test_') else 'calls'
                        self._bind(caller, dest, relation,
                                   [self.snapshot.anchor(file, item.lineno, item.end_lineno),
                                    *extra, *self.nodes[dest]['anchors']])
                elif isinstance(item, (ast.Name, ast.Attribute)) and isinstance(item.ctx, ast.Load):
                    name = _qualified_name(item)
                    if not name or name.split('.')[0] in locals_:
                        continue
                    dest = self._resolve_python(file, name)
                    if dest and self.nodes[dest].get('symbol_kind') in ('constant', 'binding'):
                        anchors = [self.snapshot.anchor(file, item.lineno, item.end_lineno),
                                   *self.nodes[dest]['anchors']]
                        self._bind(caller, dest, 'reads', anchors)
                        self._bind(dest, caller, 'read_by', anchors)

    def _other_languages(self, files):
        other = [f for f in files if PurePosixPath(f).suffix in ('.go', '.js', '.jsx', '.ts', '.tsx')]
        if not other:
            return
        try:
            from .syntax_graph import index_languages
            index_languages(self, other)
        except ImportError:
            self.unindexed_languages = sorted({PurePosixPath(f).suffix for f in other})

    def issue_edges(self, instance):
        text = instance['problem_statement']
        choices = {}
        leaves = defaultdict(list)
        for nid, node in self.nodes.items():
            if node.get('symbol_kind') not in ('literal', 'module'):
                leaves[node.get('symbol', '').rsplit('.', 1)[-1]].append(nid)
        for nid, node in sorted(self.nodes.items()):
            needles = []
            kind = node.get('symbol_kind')
            if kind == 'module':
                needles.append(node['file'])
            elif kind in ('literal','option_name'):
                value = node['value']
                if len(value) >= (4 if kind=='option_name' else 8) and not value.isspace():
                    needles.append(value)
            else:
                symbol = node.get('symbol', '')
                needles.append(symbol)
                leaf = symbol.rsplit('.', 1)[-1]
                if len(leaf) >= 4 and len(leaves[leaf]) == 1:
                    needles.append(leaf)
            for needle in needles:
                if not needle or needle.startswith('$'):
                    continue
                pattern = r'(?<![A-Za-z0-9_])' + re.escape(needle) + r'(?![A-Za-z0-9_])'
                match = re.search(pattern, text, re.I if kind=='option_name' else 0)
                if match and precise_issue_clue(text,node,match.start(),match.end()):
                    anchor = {'kind': 'issue', 'start_char': match.start(), 'end_char': match.end(),
                              'sha256': digest_string(match.group())}
                    choices[nid] = {'edge_id': 'mention-' + digest([instance['instance_id'], nid, anchor])[:16],
                                    'src': 'issue', 'dst': nid, 'relation': 'mentions',
                                    'anchors': [anchor, *node['anchors']]}
                    break
        return list(choices.values())

    def _configuration(self, files):
        for file in files:
            if PurePosixPath(file).suffix not in ('.json', '.yml', '.yaml', '.toml', '.ini', '.cfg', '.cue'):
                continue
            try:
                lines = self.snapshot.text(file).splitlines()
            except ValueError:
                continue
            if not lines:
                continue
            module = file + '::$module'
            self.nodes[module] = {'node_id': module, 'kind': 'artifact', 'file': file,
                                  'symbol': '$module', 'symbol_kind': 'module',
                                  'start_line': 1, 'end_line': len(lines),
                                  'anchors': [self.snapshot.anchor(file, 1, 1)]}
            seen = set()
            for line, text in enumerate(lines, 1):
                match = re.match(r'''\s*["']?([A-Za-z_][\w.-]*)["']?\s*[:=]''', text)
                if match and match[1] not in seen:
                    seen.add(match[1])
                    self._declare(file, match[1], line, line, 'option')

    def target_nodes(self, spans):
        """Map old-side patch spans to the smallest enclosing pre-fix declarations."""
        result = set()
        for span in spans:
            file, line = span['file'], span['start_line']
            candidates = [n for n in self.nodes.values() if n['file'] == file
                          and n.get('symbol_kind') not in ('literal', 'option_name', 'module')
                          and n['start_line'] <= line <= n['end_line']]
            if candidates:
                selected = min(candidates, key=lambda n: (n['end_line'] - n['start_line'], n['symbol']))
                result.add(selected['node_id'])
            elif file + '::$module' in self.nodes:
                result.add(file + '::$module')
        return result


def import_candidates(file, text):
    """Resolve repository-relative import locations for source acquisition."""
    if file.endswith('.py'):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return []
        names = []
        package = min(_module_names(file), key=len).split('.')
        if not file.endswith('/__init__.py'):
            package.pop()
        for node in tree.body:
            if isinstance(node, ast.Import):
                names += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    prefix = package[:len(package) - node.level + 1]
                    name = '.'.join(prefix + ([node.module] if node.module else []))
                else:
                    name = node.module or ''
                names.append(name)
        root = file.split('/')[0]
        return sorted({prefix + name.replace('.', '/') + suffix
                       for name in names if name and name.split('.')[0] == (file.split('/')[1] if root in ('src', 'lib') and '/' in file else root)
                       for prefix in (['', root + '/'] if root in ('src', 'lib') else [''])
                       for suffix in ('.py', '/__init__.py')})
    if PurePosixPath(file).suffix in ('.js', '.jsx', '.ts', '.tsx'):
        imports = re.findall(r'''(?:from\s*|require\s*\(\s*|import\s*)['"]([.][^'"]+)['"]''', text)
        return sorted({posixpath.normpath(posixpath.join(posixpath.dirname(file), value)) + suffix
                       for value in imports for suffix in ('', '.ts', '.tsx', '.js', '/index.ts', '/index.js')
                       if not value.startswith('../' * 15)})
    return []
