"""Dataset annotation uses connected pre-fix bindings and actual relation counts."""
import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ptrace.annotation import annotate, construction_observations, patch_spans, reference_selection, base_revision_targets
from ptrace.audit import audit_route, structural_errors
from ptrace.artifact_graph import ArtifactGraph
from ptrace.observed import graph_review
from ptrace.source_cache import export_source_cache, verify_source_cache
from test_source_methods import SourceSnapshot


class SourceAnnotationTests(unittest.TestCase):
    def fixture(self):
        snapshot = SourceSnapshot({
            'pkg/api.py':'from .core import compute\n\ndef entry(x):\n    return compute(x)\n',
            'pkg/core.py':'def compute(x):\n    return x+1\n\ndef compare(x):\n    return x-1\n',
        })
        instance = {'instance_id':'source-example','repo':'example/pkg','base_commit':snapshot.revision,
                    'problem_statement':'entry returns the wrong value.'}
        truth = annotate(instance,{'target_files':['pkg/core.py']},snapshot,
                         [{'file':'pkg/core.py','start_line':2}],negative_limit=5)
        return snapshot,instance,truth

    def test_reference_has_complete_source_review(self):
        snapshot,instance,truth = self.fixture()
        self.assertEqual(truth['shortest_verified_hops'],2)
        self.assertEqual(truth['hop_group'],'1-3')
        self.assertTrue(truth['graph_source_verified'])
        for route in truth['reference_paths']:
            run = {'run_id':'dataset-annotation','observations':construction_observations(instance,[route],snapshot)}
            self.assertEqual(audit_route(route,run,instance,route['source_review'],snapshot)['valid'],1)

    def test_substitutions_remain_well_formed_and_fail_binding_audit(self):
        snapshot,instance,truth = self.fixture()
        self.assertTrue(truth['hard_negative_paths'])
        for route in truth['hard_negative_paths']:
            self.assertFalse(structural_errors(route))
            run = {'run_id':'dataset-annotation','observations':construction_observations(instance,[route],snapshot)}
            check = audit_route(route,run,instance,route['source_review'],snapshot)
            self.assertEqual(check['valid'],0)
            self.assertEqual(check['first_wrong_hop'],route['hop_count'])

    def test_absent_issue_binding_produces_no_reference(self):
        snapshot,instance,truth = self.fixture()
        instance['problem_statement']='The user interface becomes unresponsive.'
        result=annotate(instance,truth,snapshot,[{'file':'pkg/core.py','start_line':2}])
        self.assertFalse(result['graph_source_verified'])
        self.assertEqual(result['reference_paths'],[])
        self.assertIsNone(result['shortest_verified_hops'])

    def test_patch_uses_old_revision_line_numbers(self):
        patch='diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -20,2 +20,3 @@\n a\n-old\n+new\n+extra\n'
        spans=patch_spans(patch)
        self.assertEqual({r['file'] for r in spans},{'a.py'})
        self.assertEqual(spans[0]['start_line'],21)
        self.assertEqual(spans[1]['start_line'],22)

    def test_repeated_binding_sites_are_one_reference_relation_route(self):
        first={'route_id':'a','hop_count':2,'edges':[]}
        second={'route_id':'b','hop_count':3,'edges':[]}
        self.assertEqual(len(reference_selection([first,first,second,second],3)),2)

    def test_renamed_target_uses_pre_fix_file(self):
        patch='diff --git a/old/component.ts b/new/component.tsx\nsimilarity index 90%\nrename from old/component.ts\nrename to new/component.tsx\n'
        self.assertEqual(base_revision_targets(['new/component.tsx'],patch),['old/component.ts'])

    def test_configuration_proposal_can_use_the_literal_key_identifier(self):
        snapshot=SourceSnapshot({'config.py':'import os\n\ndef configure():\n    return os.getenv("APP_UPLOAD_MODE")\n'})
        instance={'instance_id':'env','repo':'example/pkg','base_commit':snapshot.revision,
                  'problem_statement':'APP_UPLOAD_MODE is ignored.'}
        truth=annotate(instance,{'target_files':['config.py']},snapshot,[{'file':'config.py','start_line':4}])
        route=next(p for p in truth['reference_paths'] if any(e['relation']=='configured_by' for e in p['edges']))
        run={'run_id':'proposal','observations':construction_observations(instance,[route],snapshot)}
        route['nodes'][1]['symbol']='APP_UPLOAD_MODE'
        review=graph_review(instance,truth,run,route,ArtifactGraph(snapshot))
        self.assertEqual(audit_route(route,run,instance,review,snapshot)['valid'],1)

    def test_source_export_redacts_content_and_retains_anchor_integrity(self):
        snapshot,instance,truth=self.fixture()
        snapshot.content['pkg/api.py']=snapshot.content['pkg/api.py'].replace(
            'return compute(x)','return compute(x)  # Contact person@example.invalid')
        truth=annotate(instance,truth,snapshot,[{'file':'pkg/core.py','start_line':2}])
        def blob_sha(file):
            data=snapshot.text(file).encode()
            return hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
        snapshot.blob_sha=blob_sha
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder)
            with patch('ptrace.source_cache.get_snapshot',return_value=snapshot):
                count=export_source_cache([instance],[truth],{},folder/'source_spans.jsonl.gz')
            (folder/'issuechain_full.jsonl').write_text(json.dumps({**instance,**truth})+'\n')
            result=verify_source_cache(folder)
            self.assertEqual(count,result['source_spans'])
            self.assertGreater(result['redacted_spans'],0)
            with gzip.open(folder/'source_spans.jsonl.gz','rt') as stream:
                rows=[json.loads(line) for line in stream]
            self.assertTrue(all('person@example.invalid' not in row['text'] for row in rows))
            rows[0]['text']+='corrupted'
            with gzip.open(folder/'source_spans.jsonl.gz','wt') as stream:
                stream.writelines(json.dumps(row)+'\n' for row in rows)
            with self.assertRaisesRegex(ValueError,'digest mismatch'):
                verify_source_cache(folder)


if __name__ == '__main__':
    unittest.main()
