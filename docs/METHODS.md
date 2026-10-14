# Methods

| Paper component | Source | Entry point |
|---|---|---|
| Revision-grounded artifacts and directed relations | `ptrace/source_graph.py` | `SourceGraph`, `build-graph` |
| Issue anchoring and reference alternatives | `ptrace/construction.py` | `build-reference` |
| Observation-to-route map, Pi | `ptrace/observed.py` | `observed_routes`, `map-observations` |
| Eq. (1): route validity | `ptrace/audit.py` | `audit_route` |
| Eq. (2): target-paired support | `ptrace/metrics.py` | `evidence_support` |
| Eq. (3): false credit | `ptrace/metrics.py` | `score_run` |
| Eq. (4): metric-specific weighted aggregation | `ptrace/metrics.py` | `aggregate` |
| Eq. (5): directed, typed hard-negative overlap | `ptrace/audit.py`, `ptrace/metrics.py` | `edge_key`, `score_run` |
| Eq. (6): post-detour target return | `ptrace/metrics.py` | `score_run` |
| First unsupported relation and prefix support | `ptrace/audit.py`, `ptrace/observed.py` | `audit_route`, `support_timeline` |
| Generate-then-check, trap-aware, multi-hop | `ptrace/guidance.py`, `ptrace/baselines.py` | `guide` |
| Runtime prompt templates and recorded retrieval contracts | `ptrace/prompts.py`, `ptrace/prompt_templates/` | Prompt catalog |
| Auxiliary verifier reranking and scoring configuration | `ptrace/verifier.py`, `configs/verifier.json` | `rerank` |
| Eqs. (7)-(8): signed paired changes | `ptrace/metrics.py` | `paired_metrics` |
| Eq. (9): tokens per favorable FC correction | `ptrace/metrics.py` | `paired_metrics` |
| Eq. (10): failure-response labels | `ptrace/metrics.py` | `response_labels` |
| Algorithm 1: independent interventions and paired diagnosis | `ptrace/pipeline.py` | `diagnose`, `scripts/run_experiment.py` |
| Context removal and reference-relative redundancy | `ptrace/ablation.py`, `ptrace/observed.py` | `ablate-context`, `context_profile` |
| Staged and structure-aware localization | `ptrace/localization.py` | `staged_localization`, `structured_localization` |
| BM25, reranking, context and chain-selection baselines | `ptrace/baselines.py`, `ptrace/candidates.py` | `run` |
| Official-agent runtime interface | `ptrace/adapters.py`, `adapters/official_wrapper.py` | `external`, `import-native` |
| Tables 1-2, 4-12 and Figure 3 | `ptrace/reporting.py`, `scripts/plot_profiles.py` | `tables`, figure script |
| Required-test patch export | `scripts/export_swebench.py` | Harness prediction export |

## Source and observation semantics

`Snapshot` reads Git objects at `base_commit`. Source anchors contain file, line range, commit, and SHA-256. Observation checks compare returned source text and revision; prefixes use only observations at or before that step.

The Python index resolves module-level functions, import aliases, relative imports, plain class methods, and direct test calls. Shadowed bindings, decorated call targets, inherited dispatch, and dynamic attribute lookup remain unresolved. Other languages and semantic bindings use the same source-anchor review schema.

Support takes the maximum over complete audited routes ending in both the top-k predictions and the target set. Source-verified alternatives outside stored references qualify. The selected route determines PV; all observed valid routes determine TV. Relation identity uses artifact paths and symbols independently of descriptive prose.

With common eligibility and weights, `File@k = Support@k + FC@k`. Support is at most TV on the same observations. Eligible empty route sets score zero; unobserved fields use JSON `null`. Reviewed annotation sets are selected through `graph_source_verified`, `negative_source_verified`, and the corresponding verified-path fields.

Recovery counts an audited target return after the first detour. Evidence recovery additionally requires unsupported prefix evidence and supported final evidence. Per-action trace reviews supply detour and error-branch judgments; reference-relative context coverage is reported separately.

## Paired diagnosis

Each mode receives its own copy of baseline predictions and observations. The pair contract checks instance, commit, agent, model/deployment, revision, protocol, access, cutoff, weights, and input signature. Completed pairs enter changes and correction counts.

FC reduction includes favorable transitions and regressions. Favorable correction counts are separate. TokPerFC divides total token change over the eligible correction-and-cost cohort by favorable corrections; it is `null` when that count is zero. Support responsiveness requires observed support acquisition. Persistence requires completed coverage of every tested mode; remaining cases are unresolved.

## Experiment scopes

`configs/experiments.json` fixes model labels, agent groups, guidance variants, and scopes. Full is used for Tables 1, 4, and 8. Lite is used for Tables 2, 5-7, 9-12, and Figure 3. Resource profiles use the paper's fixed token and time scales. Display model labels and actual provider deployment IDs are recorded separately.
