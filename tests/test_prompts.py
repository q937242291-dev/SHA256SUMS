"""Prompt consistency, evaluator field separation, and verifier scoring tests."""
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from ptrace.baselines import SYSTEM, run_baseline
from ptrace.guidance import MODE_INSTRUCTIONS, guidance_input
from ptrace.prompts import catalog, load_prompt
from ptrace.verifier import verifier_input, rank_candidates, run_verifier
from test_source_methods import graph_fixture


def scoring_config():
    return json.loads((Path(__file__).resolve().parents[1] / "configs/verifier.json").read_text())


class PromptTests(unittest.TestCase):
    def test_execution_uses_the_published_templates(self):
        self.assertEqual(SYSTEM, load_prompt("agent_system"))
        for mode in ("check", "trap", "multi"):
            self.assertEqual(MODE_INSTRUCTIONS[mode], load_prompt("guide_" + mode))
        self.assertEqual(len(catalog()), 12)

    def test_multi_hop_prompt_requests_source_checked_intermediates(self):
        text = load_prompt("guide_multi")
        self.assertIn("source-checked intermediate relations", text)
        self.assertIn("recorded retrieval contract", text)
        self.assertIn("evaluator-only", text)

    def test_guidance_projection_withholds_evaluator_annotations(self):
        _, instance, _, _, baseline, _ = graph_fixture()
        instance.update({"target_files": ["PRIVATE_TARGET"], "reference_paths": ["PRIVATE_GOLD"]})
        baseline["routes"][0]["source_review"] = {"label": "PRIVATE_GOLD"}
        baseline["routes"][0]["nodes"][-1]["gold_label"] = "PRIVATE_GOLD"
        baseline["observations"][0]["evaluator_label"] = "PRIVATE_GOLD"
        baseline["observations"][0]["relations"] = [{"src": "a", "dst": "b", "relation": "calls", "label": "PRIVATE_GOLD"}]
        text = json.dumps(guidance_input(instance, baseline, "multi", []))
        self.assertNotIn("PRIVATE_TARGET", text)
        self.assertNotIn("PRIVATE_GOLD", text)

    def test_recorded_action_contract_is_enforced(self):
        snapshot, instance, _, _, baseline, _ = graph_fixture()
        baseline["retrieval_contract"] = {"base_commit": instance["base_commit"],
                                          "allowed_actions": ["finish"], "max_steps": 1, "search_limit": 20}

        class Policy:
            label, model = "none", None
            tokens, tokens_observed = 0, True
            def json(self, system, user):
                return {"action": "search", "query": "compute"}

        with patch("ptrace.baselines.get_snapshot", return_value=snapshot):
            result = run_baseline(instance, {}, "ptrace", Policy(), baseline=baseline, guidance="check")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["retrieval_contract"], baseline["retrieval_contract"])
        self.assertEqual(result["observations"], baseline["observations"])

    def test_verifier_input_uses_visible_fields_and_recorded_spans(self):
        _, instance, _, _, baseline, _ = graph_fixture()
        instance["target_files"] = ["PRIVATE_TARGET"]
        baseline["routes"][0]["source_review"] = {"label": "PRIVATE_GOLD"}
        payload = verifier_input(instance, baseline, scoring_config())
        self.assertNotIn("PRIVATE_TARGET", json.dumps(payload))
        self.assertNotIn("PRIVATE_GOLD", json.dumps(payload))
        self.assertEqual(len(payload["source_spans"]), 2)
        self.assertEqual(payload["configuration"]["evidence_scope"], "baseline_observations")

    def test_verifier_ranking_uses_configured_weighted_scores(self):
        _, instance, _, _, baseline, _ = graph_fixture()
        payload = verifier_input(instance, baseline, scoring_config())
        response = {"candidates": [{"candidate_id": payload["candidates"][0]["candidate_id"],
                    "scores": {"issue_anchoring": 1, "relation_grounding": 1,
                               "evidence_coverage": 0.5, "endpoint_explanation": 0},
                    "evidence_span_ids": [s["span_id"] for s in payload["source_spans"]]}]}
        ratings, files = rank_candidates(payload, response)
        self.assertAlmostEqual(ratings[0]["score"], 0.75)
        self.assertEqual(files, ["pkg/core.py"])

    def test_verifier_cannot_cite_an_unobserved_span(self):
        _, instance, _, _, baseline, _ = graph_fixture()
        payload = verifier_input(instance, baseline, scoring_config())
        response = {"candidates": [{"candidate_id": payload["candidates"][0]["candidate_id"],
                    "scores": {field: 1 for field in scoring_config()["score_fields"]},
                    "evidence_span_ids": ["future-span"]}]}
        with self.assertRaises(ValueError):
            rank_candidates(payload, response)

    def test_auxiliary_verifier_has_separate_protocol_and_costs(self):
        _, instance, _, _, baseline, _ = graph_fixture()
        baseline["resources"] = {"tokens": 100, "time_s": 1, "files": 2,
                                 "file_count_definition": "distinct_visible_span_files"}

        class Policy:
            label, model = "verifier-example", "deterministic-verifier"
            tokens, tokens_observed = 0, True
            def json(self, system, user):
                self.tokens += 7
                payload = json.loads(user)
                return {"candidates": [{"candidate_id": payload["candidates"][0]["candidate_id"],
                         "scores": {field: 1 for field in scoring_config()["score_fields"]},
                         "evidence_span_ids": [s["span_id"] for s in payload["source_spans"]]}]}

        result = run_verifier(instance, baseline, Policy(), scoring_config())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["protocol"], "auxiliary_verifier_reranking")
        self.assertEqual(result["resources"]["tokens"], 107)
        self.assertEqual(result["verifier_resources"]["tokens"], 7)
        self.assertEqual(result["observations"], baseline["observations"])


if __name__ == "__main__":
    unittest.main()
