"""Reference routes and grounded substitutions from pre-fix source bindings."""
import copy
import difflib
import re
from collections import Counter, defaultdict, deque

from .artifact_graph import ArtifactGraph
from .audit import audit_route, structural_errors
from .construction import issue_anchor
from .io import digest
from .observed import graph_review
from .privacy import redact_text


def patch_spans(patch):
    """Locate modified old-side lines in a unified patch."""
    spans, file, line = [], None, None
    for text in patch.splitlines():
        if text.startswith('diff --git '):
            match = re.match(r'diff --git a/(.*?) b/', text)
            file = match[1] if match else None
            line = None
        elif text.startswith('@@'):
            match = re.match(r'@@ -(\d+)(?:,(\d+))? \+', text)
            line = int(match[1]) if match else None
        elif file and line is not None:
            if text.startswith(('-', '+')):
                spans.append({'file': file, 'start_line': max(1, line), 'end_line': max(1, line)})
            if not text.startswith(('+', '\\')):
                line += 1
    return spans


def base_revision_targets(files,patch):
    """Map renamed editing targets to the paths in the pre-fix repository."""
    renames={}
    for line in patch.splitlines():
        if line.startswith('diff --git '):
            match=re.match(r'diff --git a/(.*?) b/(.*)$',line)
            if match and match[1]!=match[2]:
                renames[match[2]]=match[1]
    return sorted({renames.get(file,file) for file in files})


def routes_to_nodes(graph, instance, endpoints, limit=40, max_hops=12):
    """Enumerate bounded alternatives in increasing relation count."""
    reverse = defaultdict(list)
    for edge in graph.edges:
        reverse[edge['dst']].append(edge['src'])
    reachable = set(endpoints)
    back = deque(endpoints)
    while back:
        for source in reverse[back.popleft()]:
            if source not in reachable:
                reachable.add(source)
                back.append(source)
    queue = deque(([e['dst']], [e]) for e in graph.issue_edges(instance) if e['dst'] in reachable)
    visits, result, identities = Counter(), [], set()
    while queue and len(result) < limit:
        nodes, edges = queue.popleft()
        last = nodes[-1]
        visits[last] += 1
        if visits[last] > 6:
            continue
        if last in endpoints:
            identity = digest([nodes, [e['relation'] for e in edges]])[:16]
            if identity in identities:
                continue
            identities.add(identity)
            result.append({'route_id': 'ref-' + identity,
                           'nodes': [{'node_id':'issue', 'kind':'issue'}] + [dict(graph.nodes[n]) for n in nodes],
                           'edges': copy.deepcopy(edges), 'hop_count': len(edges)})
            continue
        if len(edges) < max_hops:
            for edge in sorted(graph.adjacency[last], key=lambda e:(e['relation'],e['dst'])):
                if edge['dst'] in reachable and edge['dst'] not in nodes:
                    queue.append((nodes + [edge['dst']], edges + [edge]))
    return result


def construction_observations(instance, paths, snapshot):
    """Read the source context used by a dataset annotation."""
    spans = {}
    for route in paths:
        for edge in route['edges']:
            for anchor in edge.get('anchors', []):
                if anchor['kind'] == 'source':
                    key = (anchor['file'],anchor['start_line'],anchor['end_line'])
                    spans[key] = {'file':key[0], 'start_line':key[1], 'end_line':key[2],
                                  'text':redact_text(snapshot.span(*key))}
    return [{'step':i+1, 'base_commit':instance['base_commit'], 'spans':[span]}
            for i, span in enumerate(spans.values())]


def verify_references(instance, truth, graph, paths):
    observations = construction_observations(instance, paths, graph.snapshot)
    run = {'run_id':'dataset-annotation', 'observations':observations}
    verified = []
    for route in paths:
        review = graph_review(instance,truth,run,route,graph)
        if audit_route(route,run,instance,review,graph.snapshot)['valid'] == 1:
            verified.append({**route, 'source_verified':True, 'source_review':review})
    return verified


def reference_selection(paths, count=3):
    paths=list({p['route_id']:p for p in paths}.values())
    if len(paths) <= count:
        return paths
    selected = [paths[0]]
    while len(selected) < count:
        remaining = [p for p in paths if p not in selected]
        def novelty(path):
            edges = {(e['src'],e['relation'],e['dst']) for e in path['edges']}
            old = [{(e['src'],e['relation'],e['dst']) for e in p['edges']} for p in selected]
            distance = min(len(edges ^ prior) / max(1,len(edges | prior)) for prior in old)
            return (distance, -path['hop_count'], path['route_id'])
        selected.append(max(remaining,key=novelty))
    return selected


SECURITY = re.compile(r'(?:auth|csrf|permission|password|sanitize|escape|encrypt|decrypt|secure|xss)',re.I)


def negative_routes(instance, truth, graph, references, count=12):
    """Substitute a real artifact at an explicitly bound reference call site."""
    nodes = [n for n in graph.nodes.values() if n.get('symbol_kind') in
             ('function','method','class','constant','binding','option')]
    result, identities = [], set()
    for path in references:
        sequence = [path['edges'][0]['src']] + [e['dst'] for e in path['edges']]
        for position in reversed(range(len(path['edges']))):
            actual = path['edges'][position]
            destination = graph.nodes[actual['dst']]
            def preference(node):
                a,b = destination.get('symbol',''),node.get('symbol','')
                similarity = difflib.SequenceMatcher(None,a.lower(),b.lower()).ratio()
                return (node['file']==destination['file'], similarity, node['node_id'])
            ranked = sorted(nodes,key=preference,reverse=True)
            security = [n for n in ranked if SECURITY.search(n.get('symbol',''))]
            other_files = [n for n in ranked if n['file']!=destination['file']]
            candidates = ranked[:6] + security[:2] + other_files[:4] + ranked[6:]
            for replacement in candidates:
                rid = replacement['node_id']
                if rid in sequence[:position+2]:
                    continue
                if actual['src'] != 'issue' and any(e['dst']==rid and e['relation']==actual['relation']
                                                    for e in graph.adjacency[actual['src']]):
                    continue
                if actual['src'] == 'issue':
                    symbol = replacement.get('symbol','')
                    anchor = actual['anchors'][0]
                    visible = instance['problem_statement'][anchor['start_char']:anchor['end_char']]
                    if visible in (symbol,symbol.rsplit('.',1)[-1],replacement.get('value')):
                        continue
                key = tuple((e['src'],e['relation'],e['dst']) for e in path['edges'][:position]) + ((actual['src'],actual['relation'],rid),)
                if key in identities:
                    continue
                identities.add(key)
                prefix = copy.deepcopy(path['edges'][:position])
                anchors = copy.deepcopy(actual['anchors']) + copy.deepcopy(replacement['anchors'])
                edge = {'edge_id':'counter-' + digest(key)[:16], 'src':actual['src'], 'dst':rid,
                        'relation':actual['relation'], 'anchors':anchors}
                original_name, new_name = destination.get('symbol',''),replacement.get('symbol','')
                similarity = difflib.SequenceMatcher(None,original_name.lower(),new_name.lower()).ratio()
                mechanism = ('Security FP' if SECURITY.search(new_name) else
                             'Configuration' if actual['relation'] in ('reads','read_by') else
                             'Lexical' if similarity >= .6 else 'Structural')
                route = {'route_id':'neg-' + digest(key)[:16], 'nodes':copy.deepcopy(path['nodes'][:position+1]) + [copy.deepcopy(replacement)],
                         'edges':prefix + [edge], 'hop_count':position+1,
                         'negative_mechanism':mechanism, 'source_verified':True,
                         'counterexample':{'expected_binding':copy.deepcopy(actual), 'substituted_node':rid}}
                review = copy.deepcopy(path['source_review'])
                review['route_id'] = route['route_id']
                review['endpoint_consistent'] = replacement['file'] in truth['target_files']
                used = {n['node_id'] for n in route['nodes']}
                review['node_eligibility'] = {nid:True for nid in used}
                review['edge_support'] = {e['edge_id']:review['edge_support'][e['edge_id']] for e in prefix}
                review['edge_support'][edge['edge_id']] = {'supported':False, 'anchors':anchors,
                                                          'judgment_source':'substituted_static_binding'}
                route['source_review'] = review
                if not structural_errors(route):
                    result.append(route)
                if len(result) >= count:
                    return result
    return result


def mechanism_labels(instance, references, negatives):
    """Describe the source mechanism visible in the reference and its substitutions."""
    if not references:
        return None, []
    available, evidence = set(), []
    for path in references:
        byid = {n['node_id']:n for n in path['nodes']}
        for edge in path['edges']:
            dest = byid[edge['dst']]
            relation = edge['relation']
            configuration_context = bool(re.search(r'(?:config|settings|options|rcsetup)',
                                                   ' '.join(n.get('file','') for n in path['nodes']),re.I))
            if (relation=='configured_by' or (relation in ('reads','read_by') and configuration_context
                    and any(n.get('symbol_kind') in ('constant','option','option_name') for n in path['nodes']))):
                available.add('Configuration')
                evidence.append({'category':'Configuration','route_id':path['route_id'],'edge_id':edge['edge_id']})
            if relation == 'test_calls' or (relation == 'defined_in' and byid[edge['src']].get('symbol_kind')=='literal'):
                available.add('Symptom')
                evidence.append({'category':'Symptom','route_id':path['route_id'],'edge_id':edge['edge_id']})
            if relation in ('calls','inherits','defines_method'):
                available.add('Structural')
                evidence.append({'category':'Structural','route_id':path['route_id'],'edge_id':edge['edge_id']})
    for negative in negatives:
        mechanism = negative['negative_mechanism']
        if mechanism == 'Lexical':
            available.add('Lexical')
        elif mechanism == 'Security FP' and SECURITY.search(instance['problem_statement']):
            available.add('Security FP')
        else:
            continue
        evidence.append({'category':mechanism,'route_id':negative['route_id'],
                         'edge_id':negative['edges'][-1]['edge_id']})
    priority = ('Security FP','Configuration','Symptom','Lexical','Structural')
    label = next((category for category in priority if category in available),'None')
    return label,evidence


def annotate(instance, truth, snapshot, spans, reference_limit=3, negative_limit=12, files=None):
    graph = ArtifactGraph(snapshot,files=files)
    endpoints = graph.target_nodes(spans)
    paths = routes_to_nodes(graph,instance,endpoints)
    paths = verify_references(instance,truth,graph,paths)
    selected = reference_selection(paths,reference_limit)
    negatives = negative_routes(instance,truth,graph,selected,negative_limit)
    category,evidence = mechanism_labels(instance,selected,negatives)
    shortest = min((p['hop_count'] for p in paths),default=None)
    hop_group = None if shortest is None else '1-3' if shortest<=3 else '4-5' if shortest<=5 else '6+'
    annotations = [{'artifact_type':'code','file':graph.nodes[n]['file'],'symbol':graph.nodes[n]['symbol'],
                    'start_line':graph.nodes[n]['start_line'],'end_line':graph.nodes[n]['end_line'],'role':'root_cause'}
                   for n in sorted(endpoints)]
    return {'instance_id':instance['instance_id'], 'target_files':truth['target_files'],
            'target_annotations':annotations, 'reference_paths':selected, 'hard_negative_paths':negatives,
            'verified_reference_paths':selected, 'verified_negative_paths':negatives,
            'graph_source_verified':bool(selected), 'negative_source_verified':bool(negatives),
            'shortest_verified_hops':shortest, 'hop_group':hop_group, 'verified_hop_group':hop_group,
            'category':category, 'verified_category':category, 'category_evidence':evidence,
            'category_provenance':'static_source_binding', 'hop_group_provenance':'shortest_verified_source_route',
            'annotation_method':'pre_fix_source_bindings_v2'}
