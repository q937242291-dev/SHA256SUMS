"""Independent examples test the paper equations and evidence boundary."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from ptrace.audit import audit_route, structural_errors, anchor_observed
from ptrace.metrics import score_run, aggregate, paired_metrics, response_labels, baseline_signature
from ptrace.io import public_instance, unique_files
from ptrace.privacy import sanitize, scan
from ptrace.dataset import build_splits
from ptrace.construction import annotation_agreement
from ptrace.candidates import make_pool


class MemorySnapshot:
    def __init__(self):
        self.content = {"a.py": "def entry():\n    return target()\n\ndef target():\n    return 1\n", "b.py": "def alternative():\n    return 2\n"}

    def text(self, file):
        if file not in self.content:
            raise ValueError("missing source")
        return self.content[file]

    def span(self, file, start, end):
        lines = self.text(file).splitlines()
        if start < 1 or end > len(lines) or start > end:
            raise ValueError("bad span")
        return "\n".join(lines[start-1:end])


def fixture():
    snapshot = MemorySnapshot()
    inst = {"instance_id": "i1", "repo": "public/repo", "base_commit": "a"*40, "problem_statement": "entry gives wrong output"}
    truth = {"instance_id": "i1", "target_files": ["a.py"], "category": "Symptom", "hop_group": "1-3"}
    route = {"route_id": "r1", "nodes": [{"node_id": "q", "kind": "issue"}, {"node_id": "a", "kind": "artifact", "file": "a.py", "symbol": "target"}],
             "edges": [{"edge_id": "e1", "src": "q", "dst": "a", "relation": "mentions"}]}
    span = {"file": "a.py", "start_line": 1, "end_line": 5, "text": snapshot.span("a.py",1,5)}
    run = {"run_id": "base1", "instance_id": "i1", "base_commit": inst["base_commit"], "agent": "example", "model": "none",
           "condition": "baseline", "status": "completed", "protocol": "native", "access_contract": "pre_fix_source_only_v1",
           "predicted_files": ["a.py"], "routes": [route], "selected_route_id": "r1", "weight": 1,
           "observations": [{"step": 1, "base_commit": inst["base_commit"], "spans": [span]}],
           "resources": {"tokens": 10, "files": 1, "time_s": 2, "file_count_definition": "distinct_visible_span_files"}}
    anchor = {"kind": "source", "file": "a.py", "start_line": 1, "end_line": 5, "base_commit": inst["base_commit"], "sha256": hashlib.sha256(span["text"].encode()).hexdigest()}
    review = {"run_id": "base1", "route_id": "r1", "independent": True, "reviewer_id": "reviewer-A", "issue_anchored": True,
              "endpoint_consistent": True, "node_eligibility": {"q": True, "a": True}, "edge_support": {"e1": {"supported": True, "anchors": [anchor]}}}
    return inst, truth, run, review, snapshot


def scored(inst, truth, run, review, snapshot, trace=None):
    reviews = {(run["run_id"], "r1"): review}
    if trace:
        reviews[(run["run_id"], "__trace__")] = trace
    return score_run(inst, truth, run, reviews, snapshot)


class EvidenceTests(unittest.TestCase):
    def test_same_target_is_supported(self):
        i,t,r,v,s=fixture(); z=scored(i,t,r,v,s)
        self.assertEqual((z["File@5"],z["Support@5"],z["FC@5"],z["TV"],z["PV@1"]),(1,1,0,1,1))

    def test_return_to_correct_file_without_binding_is_false_credit(self):
        i,t,r,v,s=fixture(); v["edge_support"]["e1"]["supported"]=False
        z=scored(i,t,r,v,s)
        self.assertEqual((z["File@5"],z["Support@5"],z["FC@5"]),(1,0,1))

    def test_valid_route_to_another_file_does_not_support_hit(self):
        i,t,r,v,s=fixture()
        r["routes"][0]["nodes"][1]["file"]="b.py"
        span={"file":"b.py","start_line":1,"end_line":2,"text":s.span("b.py",1,2)}
        r["observations"][0]["spans"].append(span)
        v["edge_support"]["e1"]["anchors"]=[{"kind":"source","file":"b.py","start_line":1,"end_line":2,"base_commit":i["base_commit"],"sha256":hashlib.sha256(span["text"].encode()).hexdigest()}]
        z=scored(i,t,r,v,s)
        self.assertEqual((z["TV"],z["PV@1"],z["Support@5"],z["FC@5"]),(1,1,0,1))

    def test_future_observation_does_not_support_prefix(self):
        i,t,r,v,s=fixture(); r["observations"][0]["step"]=3
        self.assertEqual(audit_route(r["routes"][0],r,i,v,s,prefix=2)["valid"],0)
        self.assertEqual(audit_route(r["routes"][0],r,i,v,s,prefix=3)["valid"],1)

    def test_source_review_cannot_supply_unread_source(self):
        i,t,r,v,s=fixture(); r["observations"] = []
        self.assertEqual(scored(i,t,r,v,s)["Support@5"],0)

    def test_source_digest_rejects_changed_revision(self):
        i,t,r,v,s=fixture(); s.content["a.py"] = s.content["a.py"].replace("return 1","return 2")
        self.assertEqual(scored(i,t,r,v,s)["Support@5"],0)

    def test_missing_observations_are_unknown_not_zero(self):
        i,t,r,v,s=fixture(); r["observations"]=None
        z=scored(i,t,r,v,s)
        self.assertIsNone(z["Support@5"]); self.assertIsNone(z["FC@5"]); self.assertEqual(z["File@5"],1)

    def test_missing_review_is_unknown(self):
        i,t,r,v,s=fixture(); z=scored(i,t,r,None,s)
        self.assertIsNone(z["PV@1"])

    def test_eligible_empty_route_set_is_zero(self):
        i,t,r,v,s=fixture(); r["routes"]=[]; r["selected_route_id"]=None
        z=scored(i,t,r,v,s)
        self.assertEqual((z["TV"],z["Support@5"],z["FC@5"]),(0,0,1))

    def test_disconnected_fragments_cannot_form_a_route(self):
        i,t,r,v,s=fixture(); route=r["routes"][0]
        route["nodes"].append({"node_id":"other","kind":"issue"})
        route["edges"].append({"edge_id":"e2","src":"other","dst":"a","relation":"calls"})
        self.assertIn("disconnected_route",structural_errors(route))
        self.assertEqual(scored(i,t,r,v,s)["PV@1"],0)

    def test_source_verified_alternative_need_not_be_in_gold_list(self):
        i,t,r,v,s=fixture(); t["reference_paths"] = []
        self.assertEqual(scored(i,t,r,v,s)["Support@5"],1)

    def test_multiple_alternatives_credit_issue_once(self):
        i,t,r,v,s=fixture(); rr=copy.deepcopy(r["routes"][0]); rr["route_id"]="r2"; r["routes"].append(rr)
        vs={(r["run_id"],"r1"):v,(r["run_id"],"r2"):v}
        self.assertEqual(score_run(i,t,r,vs,s)["Support@5"],1)

    def test_hard_negative_overlap_does_not_establish_invalidity(self):
        i,t,r,v,s=fixture(); t["hard_negative_paths"]=[r["routes"][0]]
        z=scored(i,t,r,v,s)
        self.assertEqual(z["HNegOv"],1); self.assertEqual(z["PV@1"],1)

    def test_recovery_is_a_post_detour_target_return(self):
        i,t,r,v,s=fixture(); r["observations"]=[{"step":1,"base_commit":i["base_commit"],"spans":[]}, {**r["observations"][0],"step":2}]
        trace={"independent":True,"complete":True,"detour_steps":[1]}
        v["edge_support"]["e1"]["supported"]=False
        z=scored(i,t,r,v,s,trace)
        self.assertEqual(z["Recovery"],1); self.assertEqual(z["RecoveryAfterDetour"],1)
        self.assertEqual(z["EvidenceRecoveryAfterDetour"],0)

    def test_no_detour_has_no_conditional_recovery_denominator(self):
        i,t,r,v,s=fixture(); z=scored(i,t,r,v,s,{"independent":True,"complete":True,"detour_steps":[]})
        self.assertEqual(z["Recovery"],0); self.assertIsNone(z["RecoveryAfterDetour"])

    def test_case_sensitive_paths(self):
        self.assertEqual(unique_files(["A.py","a.py","A.py"]),["A.py","a.py"])


class PairedTests(unittest.TestCase):
    def base(self, iid="i1", fc=1, support=0, pv=0, tokens=100):
        return {"run_id":iid,"instance_id":iid,"base_commit":"a"*40,"agent":"A","model":"M","condition":"baseline", "status":"completed","protocol":"native","access_contract":"v1","k":5,"weight":1,
                "File@5":fc+support,"FC@5":fc,"Support@5":support,"PV@1":pv,"Tokens":tokens,"Files":2,"Time":10,"EdgeF1":.1,"input_signature":"same-"+iid,"file_count_definition":"distinct"}

    def guide(self,b,**kw):
        g=copy.deepcopy(b); g.update({"run_id":b["run_id"]+"-check","condition":"check","baseline_run_id":b["run_id"],"baseline_input_signature":b["input_signature"]});g.update(kw);return g

    def test_withdrawal_and_acquisition_have_different_support(self):
        b=self.base(); g=self.guide(b,**{"File@5":0,"FC@5":0,"Support@5":0,"Tokens":110})
        result,responses=paired_metrics([b],[g]);z=result[0]
        self.assertEqual(z["FCred"],100); self.assertEqual(z["DeltaSupport@5"],0)
        g["File@5"]=g["Support@5"]=1
        result,_=paired_metrics([b],[g]);self.assertEqual(result[0]["DeltaSupport@5"],100)

    def test_net_change_and_favorable_count_are_distinct(self):
        b1,b2=self.base("i1"),self.base("i2",0,1,1)
        g1=self.guide(b1,**{"FC@5":0,"Support@5":1,"PV@1":1,"Tokens":120})
        g2=self.guide(b2,**{"FC@5":1,"Support@5":0,"PV@1":0,"Tokens":140})
        rr,_=paired_metrics([b1,b2],[g1,g2]);z=rr[0]
        self.assertEqual((z["FCred"],z["C_FCred"],z["C_FCregression"]),(0,1,1))
        self.assertEqual(z["TokPerFC"],60)

    def test_zero_corrections_ratio_is_undefined(self):
        b=self.base();g=self.guide(b,Tokens=130)
        rr,_=paired_metrics([b],[g]);self.assertIsNone(rr[0]["TokPerFC"])

    def test_incomplete_modes_are_unresolved(self):
        b=self.base(); responses={b["run_id"]:{"check":0}}
        self.assertEqual(response_labels([b],responses,{b["run_id"]:["check","trap"]})[0]["label"],"unresolved")
        responses[b["run_id"]]["trap"]=0
        self.assertEqual(response_labels([b],responses,{b["run_id"]:["check","trap"]})[0]["label"],"persistent")
        responses[b["run_id"]]["check"]=1
        self.assertEqual(response_labels([b],responses,{b["run_id"]:["check","trap"]})[0]["label"],"support-responsive")

    def test_pair_contract_mismatch_is_rejected(self):
        b=self.base();g=self.guide(b,access_contract="another")
        with self.assertRaises(ValueError):paired_metrics([b],[g])

    def test_pair_input_signature_mismatch_is_rejected(self):
        b=self.base();g=self.guide(b,baseline_input_signature="other")
        with self.assertRaises(ValueError):paired_metrics([b],[g])

    def test_pair_missing_field_uses_its_own_mask(self):
        b=self.base();g=self.guide(b,**{"Support@5":None,"FC@5":None,"PV@1":1})
        rr,_=paired_metrics([b],[g]);z=rr[0]
        self.assertIsNone(z["FCred"]); self.assertEqual(z["DeltaPV@1"],100)
        self.assertEqual(z["FC@5_pair_n"],0)

    def test_weighted_common_mask_identity(self):
        b1,b2=self.base("i1"),self.base("i2",0,1,1)
        b2["weight"]=3
        z=aggregate([b1,b2])["CommonMask@5"]
        self.assertEqual((z["File@5"],z["Support@5"],z["FC@5"]),(100,75,25))

    def test_duplicate_mode_cannot_be_counted_twice(self):
        b=self.base();g=self.guide(b)
        with self.assertRaises(ValueError):paired_metrics([b],[g,g])


class DataBoundaryTests(unittest.TestCase):
    def test_public_projection_withholds_gold_patch_and_labels(self):
        x={"instance_id":"i","repo":"x/y","base_commit":"a"*40,"problem_statement":"bug","target_files":["answer.py"],"patch":"answer", "reference_paths":["answer"],"category":"secret-label"}
        self.assertEqual(set(public_instance(x)),{"instance_id","repo","base_commit","problem_statement"})

    def test_source_variants_do_not_cross_train_test(self):
        instances=[{"instance_id":"a","repo":"x/y","source_instance_id":"same"},{"instance_id":"b","repo":"x/y","source_instance_id":"same"}]
        splits=build_splits(instances)
        for scheme in ("grouped_standard","heldout_repository"):
            containing=[s for s,ids in splits[scheme].items() if "a" in ids]
            self.assertIn("b",splits[scheme][containing[0]])

    def test_blinded_candidates_remove_reference_ids_and_labels(self):
        i,t,r,v,s=fixture();t["reference_paths"]=r["routes"];t["hard_negative_paths"]=[{**r["routes"][0],"negative_mechanism":"hidden"}]
        pool,labels=make_pool(i,t)
        self.assertEqual(set(pool[0]),{"path_id","nodes","edges"})
        self.assertNotIn("source_verified",pool[0]);self.assertTrue(all(x["path_id"].startswith("p-") for x in pool))
        self.assertEqual(set(labels.values()),{"reference","distractor"})

    def test_agreement_uses_real_binary_rows_only(self):
        z=annotation_agreement([{"reference":True,"annotation":True},{"reference":False,"annotation":False},{"reference":True,"annotation":None}])
        self.assertEqual((z["n"],z["agreement"],z["kappa"]),(2,1,1))

    def test_redactor_removes_secret_without_printing_it(self):
        value="s"+"k-"+"X"*36
        self.assertNotIn(value,sanitize({"text":value})["text"])


if __name__ == "__main__":
    unittest.main()
