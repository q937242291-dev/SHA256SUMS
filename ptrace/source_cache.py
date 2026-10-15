"""Integrity checks for dataset source spans and their route anchors."""
import gzip
import hashlib
import json
from pathlib import Path

from .io import read_jsonl
from .privacy import redact_text
from .repository import get_snapshot


def span_key(repo, anchor):
    return (repo,anchor['base_commit'],anchor['file'],anchor['start_line'],anchor['end_line'],anchor['sha256'])


def verify_source_cache(directory):
    directory=Path(directory)
    keys=set()
    redacted=0
    with gzip.open(directory/'source_spans.jsonl.gz','rt',encoding='utf-8') as stream:
        for line in stream:
            row=json.loads(line)
            actual=hashlib.sha256(row['text'].encode()).hexdigest()
            expected=row['display_sha256'] if row['redacted'] else row['sha256']
            if actual!=expected:
                raise ValueError('Source-cache content digest mismatch')
            key=span_key(row['repo'],row)
            if key in keys:
                raise ValueError('Repeated source-cache span')
            keys.add(key)
            redacted+=row['redacted']
    references=negatives=0
    for record in read_jsonl(directory/'issuechain_full.jsonl'):
        for kind in ('reference_paths','hard_negative_paths'):
            for route in record[kind]:
                for edge in route['edges']:
                    for anchor in edge.get('anchors',[]):
                        if anchor['kind']=='source':
                            if span_key(record['repo'],anchor) not in keys:
                                raise ValueError('Route anchor missing from source cache')
                        elif anchor['kind']=='issue':
                            text=record['problem_statement'][anchor['start_char']:anchor['end_char']]
                            if hashlib.sha256(text.encode()).hexdigest()!=anchor['sha256']:
                                raise ValueError('Issue anchor mismatch')
                review=route['source_review']
                support=[review['edge_support'][e['edge_id']]['supported'] for e in route['edges']]
                if kind=='reference_paths':
                    if not all(v is True for v in support):
                        raise ValueError('Reference contains an unsupported source relation')
                    references+=1
                else:
                    if support[-1] is not False or not all(v is True for v in support[:-1]):
                        raise ValueError('Negative binding counterexample mismatch')
                    negatives+=1
    return {'source_spans':len(keys),'redacted_spans':redacted,
            'reference_paths':references,'negative_paths':negatives}


def export_source_cache(instances,truths,repo_map,output):
    """Export source contexts required by the reference and negative reviews."""
    byid={r['instance_id']:r for r in truths}
    seen=set()
    output=Path(output)
    output.parent.mkdir(parents=True,exist_ok=True)
    with gzip.open(output,'wt',encoding='utf-8') as stream:
        for instance in instances:
            snapshot=get_snapshot(instance,repo_map)
            truth=byid[instance['instance_id']]
            for route in truth['reference_paths']+truth['hard_negative_paths']:
                for edge in route['edges']:
                    for anchor in edge.get('anchors',[]):
                        if anchor['kind']!='source':
                            continue
                        key=span_key(instance['repo'],anchor)
                        if key in seen:
                            continue
                        original=snapshot.span(anchor['file'],anchor['start_line'],anchor['end_line'])
                        if hashlib.sha256(original.encode()).hexdigest()!=anchor['sha256']:
                            raise ValueError('Source snapshot digest mismatch')
                        text=redact_text(original)
                        row={'repo':instance['repo'],**anchor,'git_blob':snapshot.blob_sha(anchor['file']),
                             'text':text,'redacted':text!=original,
                             'display_sha256':hashlib.sha256(text.encode()).hexdigest()}
                        stream.write(json.dumps(row,ensure_ascii=True)+'\n')
                        seen.add(key)
    return len(seen)
