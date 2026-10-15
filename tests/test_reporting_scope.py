"""Paper agent, cohort, protocol, and recognition-count reporting checks."""
import copy
import csv
import tempfile
import unittest
from pathlib import Path

from ptrace.reporting import paper_tables, summaries
from ptrace.dataset import artifact_location
import test_paper_metrics as fixtures


def baseline(iid, agent, model="DeepSeek-v3.2"):
    score = fixtures.PairedTests().base(iid)
    score.update({"agent": agent, "model": model, "category": "None", "hop_group": "1-3",
                  "TV": 1, "Recovery": 0, "Detour": 0, "RecoveryAfterDetour": None})
    return score


def rows(path):
    with open(path, encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


class PaperScopeTests(unittest.TestCase):
    def test_artifact_identifiers_include_root_files_and_qualified_symbols(self):
        self.assertEqual(artifact_location("app.py"), {"file": "app.py"})
        self.assertEqual(artifact_location("pkg/schema.py::Schema::load"),
                         {"file": "pkg/schema.py", "symbol": "Schema.load"})
        self.assertEqual(artifact_location("https://example.invalid/file.py"), {})

    def test_guidance_tables_use_swe_agent_and_recovery_uses_agentless(self):
        helper = fixtures.PairedTests()
        swe, agentless = baseline("swe", "SWE-agent"), baseline("al", "Agentless")
        guided = [helper.guide(b, **{"FC@5": 0, "Support@5": 1}) for b in (swe, agentless)]
        lexical = baseline("lex", "bm25-rerank")
        with tempfile.TemporaryDirectory() as directory:
            paper_tables([swe, agentless, lexical, *guided], ["swe", "al", "lex"], directory)
            root = Path(directory)
            self.assertEqual({r["agent"] for r in rows(root/"table2_guidance.csv")}, {"SWE-agent"})
            self.assertEqual({r["agent"] for r in rows(root/"table6_recovery.csv")}, {"Agentless"})
            self.assertEqual({r["agent"] for r in rows(root/"table1_full_agents.csv")}, {"SWE-agent", "Agentless"})
            self.assertEqual({r["agent"] for r in rows(root/"table8_context_inputs.csv")}, {"bm25-rerank"})

    def test_selected_chain_file_hits_do_not_require_trace_support(self):
        score = baseline("closed", "Codex")
        score.update({"Support@5": None, "FC@5": None, "TV": None, "PV@1": None})
        with tempfile.TemporaryDirectory() as directory:
            paper_tables([score], ["closed"], directory)
            row = rows(Path(directory)/"table4_hit_counts.csv")[0]
            self.assertEqual(row["hits"], "1")
            self.assertEqual(row["false_credit"], "")
            self.assertEqual(row["supported_hits"], "")
            self.assertEqual(rows(Path(directory)/"table5_lite_agents.csv"), [])

    def test_candidate_outcomes_are_counted_only_in_lite(self):
        selected = []
        for i, outcome in enumerate(("reference", "distractor", "invalid", "reference")):
            score = baseline("candidate-"+str(i), "candidate-selection")
            score.update({"condition": "candidate-recognition", "protocol": "unlabeled_candidate_paths",
                          "candidate_outcome": outcome})
            selected.append(score)
        with tempfile.TemporaryDirectory() as directory:
            paper_tables(selected, [s["instance_id"] for s in selected[:3]], directory)
            row = rows(Path(directory)/"table9_candidate_recognition.csv")[0]
            self.assertEqual((row["Correct"], row["Distractor"], row["Invalid"], row["recognition_n"]),
                             ("1", "1", "1", "3"))

    def test_summary_keeps_evidence_protocols_separate(self):
        a = baseline("a", "Agentless")
        b = copy.deepcopy(a)
        b.update({"run_id": "b", "protocol": "supplied_verified_context_oracle", "Support@5": 1, "FC@5": 0})
        with tempfile.TemporaryDirectory() as directory:
            result, _ = summaries([a, b], directory)
            self.assertEqual(len(result), 2)
            self.assertEqual({r["TV_minus_Support_pp"] for r in result}, {0, 100})


if __name__ == "__main__":
    unittest.main()
