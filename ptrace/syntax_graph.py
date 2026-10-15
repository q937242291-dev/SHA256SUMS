"""Tree-sitter declaration and call bindings for Go and JavaScript-family code."""
import posixpath
import re
from collections import defaultdict
from pathlib import PurePosixPath


def walk(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.named_children))


def walk_scope(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(child for child in reversed(current.named_children)
                     if child.type not in ('function_declaration', 'function_expression', 'arrow_function',
                                            'method_definition', 'class_declaration'))


def source(node):
    return node.text.decode('utf-8') if node else ''


def field(node, name):
    return node.child_by_field_name(name)


def line_span(node):
    return node.start_point.row + 1, node.end_point.row + (1 if node.end_point.column else 0)


def at_module_scope(node):
    parent=node.parent
    while parent is not None:
        if parent.type in ('function_declaration','function_expression','arrow_function','method_definition'):
            return False
        parent=parent.parent
    return True


def index_languages(graph, files):
    from tree_sitter import Language, Parser
    import tree_sitter_go
    import tree_sitter_javascript
    import tree_sitter_typescript
    parsers = {
        '.go': Parser(Language(tree_sitter_go.language())),
        '.js': Parser(Language(tree_sitter_javascript.language())),
        '.jsx': Parser(Language(tree_sitter_javascript.language())),
        '.ts': Parser(Language(tree_sitter_typescript.language_typescript())),
        '.tsx': Parser(Language(tree_sitter_typescript.language_tsx())),
    }
    units, declarations, scopes = {}, {}, {}
    packages = defaultdict(list)
    imports = defaultdict(dict)
    exports = defaultdict(dict)
    classes = {}
    config_imports=defaultdict(dict)
    for file in files:
        try:
            raw = graph.snapshot.text(file).encode('utf-8')
        except ValueError:
            continue
        tree = parsers[PurePosixPath(file).suffix].parse(raw)
        units[file] = tree.root_node
        lines = graph.snapshot.text(file).splitlines()
        if not lines:
            continue
        module = file + '::$module'
        graph.nodes[module] = {'node_id': module, 'kind': 'artifact', 'file': file,
                               'symbol': '$module', 'symbol_kind': 'module',
                               'start_line': 1, 'end_line': len(lines),
                               'anchors': [graph.snapshot.anchor(file, 1, 1)]}
        declarations[file] = {}
        if file.endswith('.go'):
            package = next((source(n).split()[-1] for n in tree.root_node.named_children
                            if n.type == 'package_clause'), '')
            packages[(posixpath.dirname(file), package)].append(file)

        def declare(node, symbol, kind, parent=None):
            start, end = line_span(node)
            nid = graph._declare(file, symbol, start, end, kind, parent)
            declarations[file][symbol] = nid
            if parent is None:
                graph._bind(module, nid, 'declares', graph.nodes[nid]['anchors'])
            if kind in ('function', 'method'):
                scopes[nid] = node
            return nid

        def visit(node, parent=None):
            typ = node.type
            if typ in ('function_declaration', 'generator_function_declaration'):
                name = source(field(node, 'name'))
                if name:
                    declare(node, name, 'function')
                    if not file.endswith('.go') and node.parent.type == 'export_statement':
                        exports[file][name] = name
                return
            if typ == 'method_declaration':
                receiver = field(node, 'receiver')
                spec = next((n for n in walk(receiver) if n.type == 'parameter_declaration'), None)
                receiver_type = source(field(spec, 'type')).lstrip('*') if spec else ''
                name = source(field(node, 'name'))
                if receiver_type and name:
                    declare(node, receiver_type + '.' + name, 'method')
                return
            if typ in ('class_declaration', 'abstract_class_declaration'):
                name = source(field(node, 'name'))
                if name:
                    cid = declare(node, name, 'class')
                    classes[cid] = node
                    for child in field(node, 'body').named_children:
                        if child.type == 'method_definition':
                            method = source(field(child, 'name'))
                            if method and re.fullmatch(r'[A-Za-z_$][\w$]*', method):
                                declare(child, name + '.' + method, 'method', cid)
                    if node.parent.type == 'export_statement':
                        exports[file][name] = name
                return
            if typ == 'type_spec':
                name = source(field(node, 'name'))
                declare(node, name, 'interface' if field(node, 'type').type == 'interface_type' else 'class')
                return
            if typ == 'variable_declarator':
                name, value = source(field(node, 'name')), field(node, 'value')
                if re.fullmatch(r'[A-Za-z_$][\w$]*', name) and value is not None:
                    # Module declarations have no function ancestor.
                    ancestor = node.parent
                    while ancestor and ancestor.type not in ('program', 'function_declaration', 'function_expression',
                                                              'arrow_function', 'method_definition'):
                        ancestor = ancestor.parent
                    if ancestor and ancestor.type == 'program':
                        kind = 'function' if value.type in ('function_expression', 'arrow_function') else 'binding'
                        declare(value if kind == 'function' else node, name, kind)
                        if node.parent.parent.type == 'export_statement':
                            exports[file][name] = name
                return
            if typ == 'assignment_expression':
                left, value = source(field(node, 'left')), field(node, 'right')
                if value and value.type in ('function_expression', 'arrow_function'):
                    if left.startswith(('exports.', 'module.exports.')):
                        name = left.removeprefix('module.').removeprefix('exports.')
                        if re.fullmatch(r'[A-Za-z_$][\w$]*', name):
                            declare(value, name, 'function')
                            exports[file][name] = name
                    elif re.fullmatch(r'[A-Za-z_$][\w$]*\.[A-Za-z_$][\w$]*', left):
                        declare(value, left, 'function')
                    return
                elif left.startswith(('exports.', 'module.exports.')) and value and value.type == 'identifier':
                    name = left.removeprefix('module.').removeprefix('exports.')
                    exports[file][name] = source(value)
            if typ in ('function_expression', 'arrow_function', 'method_definition'):
                return
            return True

        pending = [tree.root_node]
        while pending:
            node = pending.pop()
            if visit(node):
                pending.extend(reversed(node.named_children))

    def js_file(current, spec):
        if not spec.startswith('.'):
            return None
        path = posixpath.normpath(posixpath.join(posixpath.dirname(current), spec))
        candidates = [path + suffix for suffix in ('', '.ts', '.tsx', '.js', '.jsx', '/index.ts', '/index.tsx', '/index.js')]
        found = [p for p in candidates if p in units]
        return found[0] if found else None

    for file, root in units.items():
        if file.endswith('.go'):
            for node in walk(root):
                if node.type == 'import_spec':
                    path = source(field(node, 'path')).strip('"`')
                    alias = source(field(node, 'name')) or path.rsplit('/', 1)[-1]
                    imports[file][alias] = (path, '', node)
        else:
            for node in walk(root):
                if node.type == 'import_statement':
                    path = source(field(node, 'source')).strip('"\'')
                    if path in ('nconf','config'):
                        clause=next((n for n in node.named_children if n.type=='import_clause'),None)
                        if clause:
                            for name in clause.named_children:
                                if name.type=='identifier':
                                    config_imports[file][source(name)]=node
                    target = js_file(file, path)
                    if not target:
                        continue
                    for part in walk(node):
                        if part.type == 'import_specifier':
                            imported = source(field(part, 'name'))
                            alias = source(field(part, 'alias')) or imported
                            imports[file][alias] = (target, imported, node)
                        elif part.type == 'namespace_import':
                            alias = source(part.named_children[-1])
                            imports[file][alias] = (target, '*', node)
                        elif part.type == 'identifier' and part.parent.type == 'import_clause':
                            imports[file][source(part)] = (target, 'default', node)
                elif node.type == 'variable_declarator':
                    value = field(node, 'value')
                    if value and value.type == 'call_expression' and source(field(value, 'function')) == 'require':
                        args = field(value, 'arguments')
                        if args and len(args.named_children) == 1:
                            package=source(args.named_children[0]).strip('"\'')
                            name=source(field(node,'name'))
                            if package in ('nconf','config') and at_module_scope(node):
                                config_imports[file][name]=node
                            target = js_file(file, source(args.named_children[0]).strip('"\''))
                            name = source(field(node, 'name'))
                            if target and re.fullmatch(r'[A-Za-z_$][\w$]*', name):
                                imports[file][name] = (target, '*', node)

    def go_candidates(file, name, receiver=None):
        folder = posixpath.dirname(file)
        eligible = [f for f in units if f.endswith('.go') and posixpath.dirname(f) == folder]
        symbol = (receiver + '.' if receiver else '') + name
        found = [declarations[f][symbol] for f in eligible if symbol in declarations[f]]
        return found[0] if len(found) == 1 else None

    def go_import(file, alias, name):
        item = imports[file].get(alias)
        if not item:
            return None
        path = item[0]
        repo = getattr(graph.snapshot, 'repo', '')
        prefix = 'github.com/' + repo
        if not repo or not path.startswith(prefix + '/'):
            return None
        folder = path[len(prefix) + 1:]
        found = [declarations[f][name] for f in units if posixpath.dirname(f) == folder and name in declarations[f]]
        return found[0] if len(found) == 1 else None

    for caller, function in scopes.items():
        file = graph.nodes[caller]['file']
        go = file.endswith('.go')
        local_types, assignments, local_names = {}, defaultdict(int), set()
        parameters = field(function, 'parameters')
        if parameters:
            for parameter in parameters.named_children:
                name = field(parameter, 'name') or field(parameter, 'pattern')
                if name:
                    local_names.add(source(name))
                elif parameter.type == 'identifier':
                    local_names.add(source(parameter))
        for node in walk_scope(function):
            if node.type in ('short_var_declaration', 'assignment_statement', 'assignment_expression', 'variable_declarator'):
                name = field(node, 'left') or field(node, 'name')
                if name:
                    assignments[source(name)] += 1
                    if node.type in ('short_var_declaration', 'variable_declarator'):
                        local_names.update(re.findall(r'[A-Za-z_$][\w$]*', source(name)))
            if go and node.type == 'parameter_declaration':
                name, typ = source(field(node, 'name')), source(field(node, 'type')).lstrip('*')
                if name and re.fullmatch(r'[A-Za-z_]\w*', typ):
                    local_types[name] = typ
            if not go and node.type == 'variable_declarator':
                value = field(node, 'value')
                if value and value.type == 'new_expression':
                    local_types[source(field(node, 'name'))] = source(field(value, 'constructor'))
        for node in walk_scope(function):
            if node.type != 'call_expression':
                continue
            expr = field(node, 'function')
            name = source(expr)
            dest, extra = None, []
            arguments=field(node,'arguments')
            if arguments and arguments.named_children and '.' in name:
                root,method=name.split('.',1)
                statement=None
                if go and root in imports[file] and root not in local_names:
                    package,_,candidate=imports[file][root]
                    if (package=='os' and method in ('Getenv','LookupEnv')) or (
                            package.endswith('/viper') and method in ('Get','GetString','GetBool','GetInt','GetDuration','SetDefault')):
                        statement=candidate
                elif not go and root in config_imports[file] and root not in local_names and method=='get':
                    statement=config_imports[file][root]
                argument=arguments.named_children[0]
                if statement is not None and argument.type in ('string','interpreted_string_literal','raw_string_literal'):
                    key=source(argument).strip('"\'`')
                    if '\\' not in key:
                        graph._option_value(file,caller,key,*line_span(node),
                                            import_anchors=[graph.snapshot.anchor(file,*line_span(statement))])
            if go:
                if expr.type == 'identifier' and name not in local_names:
                    dest = go_candidates(file, name)
                elif expr.type == 'selector_expression':
                    root, method = source(field(expr, 'operand')), source(field(expr, 'field'))
                    if root in local_types and assignments[root] == 0:
                        dest = go_candidates(file, method, local_types[root])
                    elif root in imports[file] and root not in local_names:
                        dest = go_import(file, root, method)
                        if dest:
                            extra.append(graph.snapshot.anchor(file, *line_span(imports[file][root][2])))
            else:
                parts = name.split('.')
                if len(parts) == 1 and name in declarations[file] and assignments[name] == 0 and name not in local_names:
                    dest = declarations[file][name]
                elif parts[0] in imports[file] and parts[0] not in local_names:
                    target, imported, statement = imports[file][parts[0]]
                    wanted = parts[1] if imported == '*' and len(parts) == 2 else imported if len(parts) == 1 else None
                    if wanted:
                        symbol = exports[target].get(wanted)
                        if symbol and symbol in declarations[target]:
                            dest = declarations[target][symbol]
                            extra.append(graph.snapshot.anchor(file, *line_span(statement)))
                elif len(parts) == 2 and parts[0] in local_types and assignments[parts[0]] == 1:
                    dest = declarations[file].get(local_types[parts[0]] + '.' + parts[1])
                elif len(parts) == 2 and parts[0] == 'this' and '.' in graph.nodes[caller]['symbol']:
                    cls = graph.nodes[caller]['symbol'].split('.')[0]
                    dest = declarations[file].get(cls + '.' + parts[1])
                elif name in declarations[file]:
                    dest = declarations[file][name]
            if dest and graph.nodes[dest].get('symbol_kind') in ('function', 'method'):
                relation = 'test_calls' if re.search(r'(?:test|spec)', file, re.I) else 'calls'
                graph._bind(caller, dest, relation,
                            [graph.snapshot.anchor(file, *line_span(node)), *extra, *graph.nodes[dest]['anchors']])
        for node in walk_scope(function):
            if node.type in ('interpreted_string_literal', 'raw_string_literal', 'string'):
                value = source(node).strip('"\'`')
                if '\\' not in value:
                    graph._value(file, caller, value, *line_span(node))
