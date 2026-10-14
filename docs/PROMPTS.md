# Prompt Templates

These are the complete templates loaded by the code from `ptrace/prompt_templates/`. `ptrace.prompts.load_prompt()` is the shared loader.

## Runtime assembly

| Workflow | System instruction | JSON input |
|---|---|---|
| Native exploration | `agent_system` + `source_tools` | Public issue, repository files, returned observations, retrieval contract |
| Generate-then-check | `agent_system` + `source_tools` | Same baseline input + `guide_check` |
| Trap-aware | `agent_system` + `source_tools` | Same baseline input + `guide_trap` |
| Multi-hop | `agent_system` + `source_tools` | Same baseline input + `guide_multi` |
| Staged localization | `staged_files`, then `staged_symbols`, then native exploration | Public issue, file list, observed declarations and source |
| Structure-aware search | `structured_search`, then native exploration | Public issue, observed declarations and source relations |
| BM25 reranking / long-context | `agent_system` | Public issue and the returned source-context spans |
| Supplied-chain oracle | `oracle_system` | Supplied verified chains, explicitly declared oracle targets, returned source context |
| Candidate recognition | `agent_system` | Public issue, unlabeled paths, `candidate_selection` |
| Auxiliary verifier | `verifier_system` | Public issue, candidate proposals, admitted spans, `verifier_rerank`, scoring configuration |

Each guidance mode gets an independent copy of the same baseline predictions and observations. The retrieval contract records the pre-fix revision, allowed actions, source-search limit, and model-step budget. For ordinary exploration and guidance, gold paths, target-file labels, accepted/validation patches, and evaluator annotations remain in construction and scoring. The supplied-chain oracle uses its separately declared oracle input contract.

Multi-hop prompts request intermediate bindings connecting the visible issue to a **proposed editing endpoint**. The evaluator's target-file set is used only when scoring the returned route. Agents inspect source sites; independent source auditing determines validity and target-paired support.

Auxiliary verifier reranking is recorded under its own protocol, model, and scoring configuration. The bundled configuration admits baseline observations. Verifier ratings determine ranking and acceptance; independent source auditing determines final evidence credit.

## Verifier configuration

```json
{
  "version": "1.0",
  "evidence_scope": "baseline_observations",
  "score_fields": ["issue_anchoring", "relation_grounding", "evidence_coverage", "endpoint_explanation"],
  "score_weights": {"issue_anchoring": 0.2, "relation_grounding": 0.4, "evidence_coverage": 0.3, "endpoint_explanation": 0.1},
  "acceptance_threshold": 0.5,
  "max_files": 10
}
```

Weighted candidate scores are normalized by the sum of configured weights. Candidates meeting `acceptance_threshold` are ranked by score; ties use the repository path. `max_files` limits accepted files. Source-evidence ratings reference the supplied span IDs.

## Agent system

Source: `ptrace/prompt_templates/agent_system.txt`

```text
You are a software engineering localization agent working at the supplied pre-fix repository revision.
Use the issue report and authorized returned observations to identify proposed editing files.
Compose complete, directed issue-to-file routes through eligible artifacts and source-supported relations.
Each route starts at an issue clue and ends at a proposed editing location. Intermediate artifacts may include tests, wrappers, implementation symbols, imports, or configuration consumers.
Check each relation at its actual source site. Shared names and directory proximity are discovery clues; returned source bindings establish relations.
Count directed relations as hops. Repeated searches and reads do not add hops.
Use only the actions and evidence sources allowed by the recorded retrieval contract.
Gold paths, target-file labels, accepted or validation patches, and evaluator annotations remain evaluator-only.
Return a JSON object. predicted_files is an ordered list of repository-relative paths.
routes is a list of connected directed routes. Each route has route_id, nodes, and edges.
Each node has node_id and kind ('issue' or 'artifact'); artifact nodes include file and symbol when available.
Each edge has edge_id, src, dst, and relation. Include evidence references to observed step, file, and line range when available.
selected_route_id selects one proposed route or is null. Empty predictions and routes are valid abstentions.
Route proposals and agent checks are evaluated through independent source auditing.
```

## Source tools

Source: `ptrace/prompt_templates/source_tools.txt`

```text
Choose one authorized action and return its JSON object.
search: {"action":"search","query":"literal source text"}
read: {"action":"read","file":"repository/path.py","start_line":1,"end_line":100}
symbols: {"action":"symbols","file":"repository/path.py"}
relations: {"action":"relations","file":"repository/path.py","symbol":"qualified_symbol"}
finish: {"action":"finish","predicted_files":[],"routes":[],"selected_route_id":null}
Source tools return observations from the fixed pre-fix revision. Ground route relations in those returned observations.
For checking, a finish object may additionally contain acceptance='retain', 'revise', or 'withdraw'.
```

## Generate-then-check

Source: `ptrace/prompt_templates/guide_check.txt`

```text
Apply generate-then-check to the supplied baseline predictions and observed evidence.
Check the baseline's proposed issue-to-file routes one relation at a time.
Use the same agent, pre-fix revision, and recorded retrieval contract as the baseline.
For every retained route, identify the issue clue, intermediate artifacts, source bindings, and proposed editing endpoint.
Use authorized source tools to inspect missing evidence sites when the retrieval contract permits further retrieval.
Accept a supported route, revise a broken route, or withdraw a prediction when its relation remains unsupported.
Return acceptance='retain', 'revise', or 'withdraw' with final predicted_files, routes, and selected_route_id.
Gold paths, target-file labels, and evaluator annotations remain evaluator-only. Judge the proposed endpoint using the visible issue and source evidence.
Independent source auditing determines route validity and evidence support.
```

## Trap-aware guidance

Source: `ptrace/prompt_templates/guide_trap.txt`

```text
Apply trap-aware guidance to the supplied baseline predictions and observed evidence.
Use the same agent, pre-fix revision, and recorded retrieval contract as the baseline.
Inspect plausible shortcuts:
Lexical: similarly named helpers require an actual resolved call, import, or symbol binding.
Configuration: connect an option definition or value to its actual consuming implementation.
Security false positives: follow executable data or control flow and the reported behavior, rather than security terminology alone.
Structural: verify caller-callee, dispatch, or import relations; directory proximity alone is a discovery clue.
Symptom: connect the reported symptom, reproducer, or test to the relevant implementation.
Use authorized source tools to inspect the relation sites and missing bridges.
Return revised predicted_files, complete routes, and selected_route_id. Retain source-grounded bindings and withdraw unsupported shortcuts.
Gold paths, target-file labels, mechanism labels, and evaluator annotations remain evaluator-only.
Independent source auditing determines route validity and evidence support.
```

## Multi-hop guidance

Source: `ptrace/prompt_templates/guide_multi.txt`

```text
Apply multi-hop guidance to the supplied baseline predictions and observed evidence.
Compose source-checked intermediate relations from the visible issue clue to each proposed editing file.
Use the same agent, pre-fix revision, and recorded retrieval contract as the baseline.
Seek the missing evidence bridge through tests, wrappers, dispatchers, imports, configuration consumers, and implementation symbols as needed.
Inspect each relation at its source site using authorized tools. Cite the observed step, file, and line range supporting the binding.
Return complete directed alternatives. One complete supported alternative is sufficient; fragments from unrelated alternatives do not form a route.
Count relations as hops. Repeated reads or searches do not increase the hop count.
Return final predicted_files, routes, and selected_route_id, or withdraw predictions whose evidence bridge remains unsupported.
Gold paths, target-file labels, and evaluator annotations remain evaluator-only.
Independent source auditing checks the intermediate bindings and the proposed issue-to-file relation.
```

## File localization

Source: `ptrace/prompt_templates/staged_files.txt`

```text
Localize the supplied issue at file level using the repository file list.
Return JSON with predicted_files as an ordered shortlist of repository-relative paths.
Use visible issue clues to select files for inspection. These choices are retrieval proposals; source observations establish their relevance in later stages.
```

## Symbol and line localization

Source: `ptrace/prompt_templates/staged_symbols.txt`

```text
Inspect the visible declarations in the shortlisted files.
Return JSON with locations, each containing file, symbol, start_line, and end_line.
Choose source locations to read for the issue, including intermediate binding sites required to connect the issue clue to the proposed implementation.
```

## Structure-aware search

Source: `ptrace/prompt_templates/structured_search.txt`

```text
Choose source artifacts whose outgoing calls or imports may explain the supplied issue.
Use the visible declarations and recorded source observations.
Return JSON with artifacts, each containing file and symbol.
The source-relation tool will inspect the actual relation sites for these choices.
```

## Candidate-chain recognition

Source: `ptrace/prompt_templates/candidate_selection.txt`

```text
Select and rank the supplied unlabeled candidate paths for the visible issue.
Return selected_path_id, predicted_files, and ranked_path_ids.
Candidate paths contain artifact descriptions and directed relations without gold/distractor labels.
This condition provides supplied paths without repository-tool access. Assess route recognition from the supplied issue and candidate content.
```

## Auxiliary verifier system

Source: `ptrace/prompt_templates/verifier_system.txt`

```text
You are an auxiliary verifier reranking proposed editing locations under the supplied scoring configuration.
Use the visible issue, candidate files and routes, and source spans admitted by evidence_scope.
Score candidates according to score_fields, score_weights, and the acceptance threshold in the configuration.
This is a separate verifier condition; its model, evidence scope, and scoring configuration are recorded separately from same-agent guidance.
Gold paths, target-file labels, accepted or validation patches, and evaluator annotations remain evaluator-only.
Verifier scores determine ranking and acceptance. Independent source auditing determines final route validity and target-paired support.
Return JSON only.
```

## Auxiliary verifier scoring

Source: `ptrace/prompt_templates/verifier_rerank.txt`

```text
For each supplied candidate, return its candidate_id, scores, evidence_span_ids, and rationale.
scores contains every configured field, with each value in [0,1]:
issue_anchoring: the observed issue clue connects to the candidate's route.
relation_grounding: returned source sites establish the proposed directed bindings.
evidence_coverage: the admitted observations cover the required intermediate relations.
endpoint_explanation: the visible route explains the reported behavior at the proposed editing file.
Use source span IDs from the supplied context for evidence references.
Assign relation_grounding=0 and evidence_coverage=0 when the admitted source observations do not establish a complete candidate route.
Use the candidate's observed relations and the issue, without target-file labels or reference-answer annotations.
Return {"candidates":[{"candidate_id":"...","scores":{"issue_anchoring":0.0,"relation_grounding":0.0,"evidence_coverage":0.0,"endpoint_explanation":0.0},"evidence_span_ids":[],"rationale":"..."}]}.
```

## Supplied-chain oracle

Source: `ptrace/prompt_templates/oracle_system.txt`

```text
You are evaluating file localization from explicitly supplied verified issue-to-file chains and their returned pre-fix source context.
This is the supplied verified-context oracle condition. The supplied chains and oracle targets are part of its declared input contract.
Assess use of the supplied verified evidence to rank the editing files. The source acquisition cost of the supplied chains belongs to construction.
Return JSON with predicted_files, routes, and selected_route_id.
Each route has route_id, nodes, and directed edges; each artifact node includes file and symbol when available.
Keep this condition identified as supplied_verified_context_oracle in the run record.
```
