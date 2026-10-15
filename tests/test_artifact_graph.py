"""Cross-language bindings, source values, and lexical ambiguity."""
import importlib.util
import unittest

from ptrace.artifact_graph import ArtifactGraph
from test_source_methods import SourceSnapshot


class ArtifactGraphTests(unittest.TestCase):
    def test_option_is_bound_to_its_runtime_reader(self):
        snapshot = SourceSnapshot({'pkg/a.py': 'UPLOAD_MODE = None\n\ndef save():\n    return UPLOAD_MODE\n'})
        graph = ArtifactGraph(snapshot)
        self.assertTrue(any(e['src'] == 'pkg/a.py::UPLOAD_MODE' and e['dst'] == 'pkg/a.py::save'
                            and e['relation'] == 'read_by' for e in graph.edges))

    def test_ambiguous_short_method_name_is_not_an_issue_binding(self):
        snapshot = SourceSnapshot({'a.py': 'class A:\n    def process(self):\n        pass\n\nclass B:\n    def process(self):\n        pass\n'})
        graph = ArtifactGraph(snapshot)
        self.assertFalse(graph.issue_edges({'instance_id': 'x', 'problem_statement': 'process fails'}))
        targets = {e['dst'] for e in graph.issue_edges({'instance_id': 'x', 'problem_statement': 'A.process fails'})}
        self.assertIn('a.py::A.process', targets)
        self.assertNotIn('a.py::B.process', targets)

    def test_nested_callback_is_not_an_outer_call(self):
        snapshot = SourceSnapshot({'a.py': 'def target():\n    return 1\n\ndef entry():\n    def inner():\n        return target()\n    return 2\n'})
        graph = ArtifactGraph(snapshot)
        self.assertFalse(any(e['src'] == 'a.py::entry' and e['relation'] == 'calls' for e in graph.edges))

    def test_decorated_target_has_no_assumed_call_binding(self):
        snapshot = SourceSnapshot({'a.py': '@replace\ndef target():\n    return 1\n\ndef entry():\n    return target()\n'})
        graph = ArtifactGraph(snapshot)
        self.assertFalse(any(e['src'] == 'a.py::entry' and e['dst'] == 'a.py::target' and e['relation'] == 'calls'
                             for e in graph.edges))

    def test_target_span_selects_implementation_scope(self):
        graph = ArtifactGraph(SourceSnapshot({'a.py': 'class A:\n    def process(self):\n        return 1\n'}))
        self.assertEqual(graph.target_nodes([{'file':'a.py', 'start_line':3}]), {'a.py::A.process'})

    def test_common_prose_does_not_anchor_configuration_or_test_literals(self):
        snapshot=SourceSnapshot({'workflow.yml':'jobs:\n  with: example\n',
                                 'a.py':'def configure():\n    return "defaults"\n'})
        graph=ArtifactGraph(snapshot)
        edges=graph.issue_edges({'instance_id':'x','problem_statement':'Configure the application with sensible defaults.'})
        self.assertFalse(edges)

    def test_environment_option_binds_to_its_reader(self):
        snapshot=SourceSnapshot({'config.py':'import os\n\ndef configure():\n    return os.environ.get("APP_UPLOAD_MODE")\n'})
        graph=ArtifactGraph(snapshot)
        instance={'instance_id':'env','problem_statement':'APP_UPLOAD_MODE is ignored.'}
        paths=graph.paths(instance,endpoints={'config.py'})
        self.assertTrue(any(any(e['relation']=='configured_by' for e in p['edges']) for p in paths))


@unittest.skipUnless(importlib.util.find_spec('tree_sitter'), 'Install requirements-source.txt')
class LanguageBindingTests(unittest.TestCase):
    def test_go_configuration_key_binds_to_viper_reader(self):
        snapshot=SourceSnapshot({'config.go':'package pkg\nimport "github.com/spf13/viper"\nfunc load() string { return viper.GetString("cache.redis.host") }\n'})
        graph=ArtifactGraph(snapshot)
        instance={'instance_id':'go-config','problem_statement':'cache.redis.host is ignored.'}
        paths=graph.paths(instance,endpoints={'config.go'})
        self.assertTrue(any(any(e['relation']=='configured_by' for e in p['edges']) for p in paths))

    def test_javascript_configuration_reader_preserves_key_role(self):
        snapshot=SourceSnapshot({'config.js':'const nconf = require("nconf");\nfunction load() { return nconf.get("minimumPostLength"); }\n'})
        graph=ArtifactGraph(snapshot)
        instance={'instance_id':'js-config','problem_statement':'minimumPostLength is ignored.'}
        roots=graph.issue_edges(instance)
        self.assertTrue(roots)
        self.assertTrue(any(graph.nodes[e['dst']]['symbol_kind']=='option_name' for e in roots))

    def test_go_receiver_resolves_same_package_method(self):
        snapshot = SourceSnapshot({'a.go': 'package pkg\ntype Backend struct {}\nfunc (b *Backend) entry() int { return b.parse() }\n',
                                   'b.go': 'package pkg\nfunc (b *Backend) parse() int { return 1 }\n'})
        graph = ArtifactGraph(snapshot)
        self.assertTrue(any(e['src'] == 'a.go::Backend.entry' and e['dst'] == 'b.go::Backend.parse'
                            and e['relation'] == 'calls' for e in graph.edges))

    def test_typescript_named_import_alias_resolves_export(self):
        snapshot = SourceSnapshot({'src/a.ts': 'import {compute as apply} from "./b";\nexport function entry(x: number) { return apply(x); }\n',
                                   'src/b.ts': 'export function compute(x: number) { return x + 1; }\n'})
        graph = ArtifactGraph(snapshot)
        self.assertTrue(any(e['src'] == 'src/a.ts::entry' and e['dst'] == 'src/b.ts::compute'
                            and e['relation'] == 'calls' for e in graph.edges))

    def test_typescript_parameter_shadowing_blocks_import_binding(self):
        snapshot = SourceSnapshot({'a.ts': 'import {compute} from "./b";\nexport function entry(compute: Function) { return compute(1); }\n',
                                   'b.ts': 'export function compute(x: number) { return x; }\n'})
        graph = ArtifactGraph(snapshot)
        self.assertFalse(any(e['src'] == 'a.ts::entry' and e['dst'] == 'b.ts::compute'
                             and e['relation'] == 'calls' for e in graph.edges))

    def test_javascript_require_binds_named_export(self):
        snapshot = SourceSnapshot({'a.js': 'const core = require("./b");\nfunction entry(x) { return core.compute(x); }\n',
                                   'b.js': 'exports.compute = function(x) { return x + 1; };\n'})
        graph = ArtifactGraph(snapshot)
        self.assertTrue(any(e['src'] == 'a.js::entry' and e['dst'] == 'b.js::compute'
                            and e['relation'] == 'calls' for e in graph.edges))


if __name__ == '__main__':
    unittest.main()
