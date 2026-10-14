# Usage

Trajectory-aware evaluation for software engineering agents, with IssueChainBench Full and Lite, localization baselines, guidance tools, and paper table/figure generation.

## Quick start

Requirements: **Python 3.10+** and **Git**. Run commands from the project directory. The core implementation uses the Python standard library.

```bash
python -m ptrace validate-data --data data
python scripts/reproduce.py
```

The offline example creates a miniature Git repository and exercises baseline, three independent guidance modes, source audits, paired metrics, and tables. Files are written to `work/example`.

Optional CLI installation and plotting dependencies:

```bash
python -m pip install -e .
python -m pip install -r requirements-figures.txt
ptrace --help
```

## Dataset

| Dataset | Task records | Repositories | Reference-path records | Hard-negative records |
|---|---:|---:|---:|---:|
| IssueChainBench Full | 1,547 | 29 | 4,641 | 19,043 |
| IssueChainBench Lite | 300 | Subset of Full | Included in each record | Included in each record |

Lite uses the following category and hop strata. Each axis totals 300 records.

| Category | Count | Hop stratum | Count |
|---|---:|---|---:|
| None | 39 | 1-3 | 151 |
| Configuration | 57 | 4-5 | 89 |
| Lexical | 60 | 6+ | 60 |
| Security FP | 45 | | |
| Structural | 54 | | |
| Symptom | 45 | | |

Lite records are identical to their corresponding Full records. Full retains source task variants; Lite selects each underlying issue once. Supervised splits group variants of the same underlying issue in one partition.

### Files

| Path | Contents |
|---|---|
| `data/issuechain_full.jsonl` | Complete task records with targets, reference paths, and hard negatives |
| `data/issuechain_lite.jsonl` | Lite task records |
| `data/instances.jsonl` | Public agent inputs |
| `data/evaluator.jsonl` | Evaluation targets, reference relations, and strata |
| `data/construction_inputs.jsonl` | Source patches, test patches, and repair-validation requirements |
| `data/lite_ids.json` | Fixed Lite instance IDs |
| `data/splits.json` | Grouped supervised and repository-held-out splits |
| `data/repository_manifest.json` | Public repositories and language information |
| `data/dataset_spec.json` | Dataset version, seed, and quotas |
| `ptrace/` | Data, source access, baselines, metrics, guidance, and analysis |
| `adapters/` | Official-agent process integration |
| `configs/` | Repository maps, agent/model settings, and experiment scopes |
| `schemas/` | JSON Schemas for routes, runs, and source reviews |
| `scripts/` | Tutorial, baseline source setup, experiment matrix, patch export, and plotting |
| `tests/` | Metric and data-interface tests |

### Reading records

Data uses UTF-8 JSONL: one JSON object per line.

```python
from ptrace.io import read_jsonl, public_instance

for record in read_jsonl("data/issuechain_lite.jsonl"):
    agent_input = public_instance(record)
    # Supply the public input view to the agent.
    print(agent_input["instance_id"], agent_input["repo"])
```

Use `instances.jsonl` for agent runs. `public_instance()` returns `instance_id`, `repo`, `base_commit`, `problem_statement`, and `language`. Evaluation uses `evaluator.jsonl`.

| Field | Description |
|---|---|
| `instance_id` | Stable task ID joining runs, scores, and paired interventions |
| `source_instance_id`, `source_dataset` | Public source identifiers for repository/harness integration |
| `repo`, `base_commit` | Repository and pre-fix commit |
| `problem_statement` | Issue description |
| `target_files` | Editing-target set |
| `reference_paths`, `hard_negative_paths` | Directed, typed reference relation records |
| `nodes`, `edges` | Route structure, linked by `node_id`, `src`, and `dst` |
| `structural_flags` | Route connectivity and endpoint checks |
| `source_verified` | Recorded source-verification status of a path |
| `verified_reference_paths` | Source-verified routes for reference-context experiments |
| `category`, `hop_group` | Task stratification labels |
| `verified_category`, `verified_hop_group` | Source-reviewed stratification labels |
| `shortest_verified_hops` | Length of the shortest verified sufficient route, when available |

Route validity and support are evaluated from source checks and the run's recorded observations. Stratification labels organize the evaluation cohort.

## Repository setup

Retrieve repositories in `data/repository_manifest.json`:

```bash
git clone https://github.com/django/django.git repos/django
```

Copy `configs/repo_map.example.json` to `configs/repo_map.json` and map repositories to local directories:

```json
{"django/django": "repos/django", "astropy/astropy": "repos/astropy"}
```

Each repository must contain the task's `base_commit` object. Fetch a specific commit with `git fetch origin <base_commit>` when needed. Source access uses `git show <base_commit>:<file>`, so the working-tree checkout can remain unchanged.

## Baselines

### Package implementations

| Method | Workflow | API required |
|---|---|---|
| `bm25` | BM25 over pre-fix source text and file paths | No |
| `bm25-rerank` | BM25 top-30, source spans, LLM reranking | Yes |
| `long-context` | Source context under a character budget | Yes |
| `oracle-chain` | Supplied verified routes and targets | Yes |
| `candidate-selection` | Unlabeled candidate-route selection and file ranking | Yes |
| `reference-staged` | File shortlist, symbol inspection, source reading | Yes |
| `reference-structured` | Structure-oriented retrieval and source tools | Yes |
| `reference-native`, `ptrace` | Interactive source-search, reading, and relation loop | Yes |

Run BM25:

```bash
python -m ptrace run --method bm25 --data data --cohort lite \
  --repo-map configs/repo_map.json --out work/bm25.jsonl
```

Use `--cohort full` for Full, `--limit 5` for a small run, or `--instance-id <ID>` for one task. The `reference-*` methods identify the implementations supplied in this package. Official agent runs use the integrations below.

### Model configuration

| Environment variable | Value |
|---|---|
| `PTRACE_API_KEY` | Provider credential |
| `PTRACE_BASE_URL` | API base URL |
| `PTRACE_MODEL` | Actual model/deployment ID |

Compatible mode appends `/chat/completions` to the base URL, normally including `/v1`. Anthropic mode appends `/messages` and is selected with `--provider anthropic`.

Example Bash setup:

```bash
read -r -s -p 'API credential: ' PTRACE_API_KEY
export PTRACE_API_KEY
export PTRACE_BASE_URL='https://YOUR_PROVIDER/v1'
export PTRACE_MODEL='YOUR_ACTUAL_DEPLOYMENT_ID'
```

`--model-label` sets the paper-facing name; `actual_model` stores the deployment ID. Paper configurations use DeepSeek-v3.2, GPT-5.5, and Claude 4.5.

```bash
python -m ptrace run --method bm25-rerank --data data --cohort lite \
  --repo-map configs/repo_map.json --model-label GPT-5.5 \
  --out work/bm25_rerank_gpt55.jsonl

python -m ptrace run --method long-context --data data --cohort lite \
  --repo-map configs/repo_map.json --model-label GPT-5.5 \
  --max-chars 60000 --out work/long_context_gpt55.jsonl

python -m ptrace run --method reference-native --data data --cohort lite \
  --repo-map configs/repo_map.json --model-label GPT-5.5 \
  --max-steps 15 --out work/reference_native_gpt55.jsonl
```

Credentials are supplied through the process environment. Run records retain actual model usage and error classes.

### Official-agent integrations

| Agent | Configuration | Runtime |
|---|---|---|
| Agentless | `configs/agents/agentless.json` | [Upstream](https://github.com/OpenAutoCoder/Agentless) |
| AutoCodeRover | `configs/agents/autocoderover.json` | [Upstream](https://github.com/AutoCodeRoverSG/auto-code-rover) |
| OpenHands | `configs/agents/openhands.json` | [Upstream](https://github.com/OpenHands/OpenHands) |
| SWE-agent | `configs/agents/sweagent.json` | [Upstream](https://github.com/SWE-agent/SWE-agent) |
| Augment | `configs/agents/augment.json` | Installed official runtime |
| Codex | `configs/agents/codex.json` | Installed official runtime |
| Claude Code | `configs/agents/claude_code.json` | Installed official runtime |

Install public upstream sources at a pinned commit:

```bash
python scripts/fetch_baselines.py --agent Agentless \
  --revision <40_character_commit_sha> --directory vendor/Agentless
```

Follow the upstream installation instructions in a separate agent environment. Set `revision` in the configuration to the installed commit/release and `command` to its JSON bridge argument array. The bridge initializes the installed agent and exports its run using `schemas/run.schema.json`.

The bridge receives this JSON object on stdin:

```json
{"instance": {"instance_id": "...", "repo": "...", "base_commit": "...", "problem_statement": "...", "language": "..."}, "condition": "baseline", "model_label": "GPT-5.5"}
```

It returns one canonical run object on stdout. `adapters/official_wrapper.py` delegates to the command array supplied in the local `PTRACE_NATIVE_ARGV` environment variable.

```bash
python -m ptrace run --method external --data data --cohort lite \
  --agent-config configs/agents/agentless.json --model-label GPT-5.5 \
  --out work/agentless_gpt55.jsonl
```

Import existing native exports:

```bash
python -m ptrace import-native --data data --cohort full --instance-id <ID> \
  --input <native_JSON_path> --agent SWE-agent --model-label GPT-5.5 \
  --revision <actual_revision> --format sweagent --out work/native_import.jsonl
```

Supported import formats are `canonical`, `sweagent`, and `agentless`. Other runtimes can export the canonical schema. File predictions enable localization scoring; source spans and routes enable trajectory-support scoring.

Run the agent/model matrix after configuring agent bridges and model environment variables:

```bash
python scripts/run_matrix.py --config configs/matrix.example.json \
  --data data --cohort full --out work/matrix_full

python scripts/run_matrix.py --config configs/matrix.example.json \
  --data data --cohort lite --guided --out work/matrix_lite_guided
```

`configs/experiments.json` contains agent/model combinations, cohort scopes, cutoff values, and category-specific guidance modes.

## Evaluation

### Run and observation formats

See `schemas/run.schema.json` and `schemas/route.schema.json`. A run contains its identity, agent/model, predictions, ordered observations, proposed routes, selected route, and resource measurements.

Example observation:

```json
{"step": 1, "base_commit": "...", "action": {"type": "read", "file": "src/example.py"}, "spans": [{"file": "src/example.py", "start_line": 10, "end_line": 12, "text": "The three source lines returned by the tool"}]}
```

Source spans use 1-based inclusive line ranges. Repository paths are case-sensitive. Routes start at an issue node and follow connected directed relations to an endpoint file. Source anchors identify the commit, file, span, and SHA-256 digest.

### Source auditing

Automatic audits cover explicit issue references and same-file direct Python calls under static name-resolution rules:

```bash
python -m ptrace evaluate --data data --cohort lite \
  --runs work/reference_native_gpt55.jsonl --repo-map configs/repo_map.json \
  --auto-audit --k 5 --out work/evaluation
```

For cross-file, configuration, test, and other source relations, supply source reviews following `schemas/review.schema.json`:

```bash
python -m ptrace review-template --runs work/agentless_gpt55.jsonl \
  --out work/reviews.jsonl

python -m ptrace evaluate --data data --cohort lite \
  --runs work/agentless_gpt55.jsonl --reviews work/reviews.jsonl \
  --repo-map configs/repo_map.json --k 5 --out work/agentless_evaluation
```

Populate `issue_anchored`, `endpoint_consistent`, `node_eligibility`, and per-edge `edge_support`. Source anchors contain `file`, `start_line`, `end_line`, `base_commit`, and `sha256`. Issue anchors contain character ranges and a digest.

Detour/Recovery trace reviews use `route_id: "__trace__"`, `independent: true`, `complete: true`, `detour_steps`, and optional `error_route_id`. Steps refer to recorded actions. Error positions follow the branch's relation order.

### Metrics

| Metric | Definition |
|---|---|
| File@k | At least one top-k prediction matches the editing-target set |
| PV@1 | Validity of the selected complete observed route, Eq. (1) |
| TV | Existence of a complete valid route in the observed trajectory |
| Support@k | A valid observed route supports the same correct target credited in the top-k predictions, Eq. (2) |
| FC@k | Correct-file credit without same-target support: `e * (1 - s)`, Eq. (3) |
| HNegOv | Predicted directed typed relations overlapping negative-route relations, Eq. (5) |
| EdgeF1 | F1 agreement between predicted and reference directed typed relations |
| Detour | Occurrence of an audited detour |
| Recovery | Return to a correct target after a detour, over eligible completed traces |
| RecoveryAfterDetour | Target return over the detour cohort, Eq. (6) |
| EvidenceRecoveryAfterDetour | Support absent at the detour prefix and acquired by completion |
| Hop1Error / MeanPosition | Hop-1 frequency / mean position of the first identifiable unsupported relation |
| MRR / NDCG@10 / FileF1@k | File-ranking and target-set metrics |
| Tokens / Files / Time | API tokens, runner-defined file count, and elapsed seconds |

Rates use percentages; MRR/NDCG/F1 use 0-1; rate changes use percentage points; time uses seconds. Every aggregate includes `eligible_n` and `weight_sum`. Null fields are excluded from that metric's denominator. Observed empty or malformed routes receive zero; zero denominators produce null.

On a common cutoff, target set, mask, and weighting scheme, `File = Support + FC`. Reports include `CommonMask@k` and `FC_over_hits@k`. Recovery describes target return; EvidenceRecoveryAfterDetour describes acquired support. In Figure 3, Return is Recovery.

Outputs: `scores.jsonl`, `summary.json`, `agent_summary.csv`, and `strata.csv`, under the specified output directory.

## Guidance and paired analysis

Modes: `check` (generate-then-check), `trap` (trap-aware), and `multi` (multi-hop). Each independently reuses the baseline issue, pre-fix access, predictions, and observations.

```bash
python -m ptrace guide --data data --cohort lite \
  --baseline work/reference_native_gpt55.jsonl --repo-map configs/repo_map.json \
  --model-label GPT-5.5 --modes check trap multi --out work/guided_gpt55.jsonl

python -m ptrace evaluate --data data --cohort lite \
  --runs work/guided_gpt55.jsonl --repo-map configs/repo_map.json \
  --auto-audit --out work/guided_evaluation

python -m ptrace pair --baseline-scores work/evaluation/scores.jsonl \
  --guided-scores work/guided_evaluation/scores.jsonl \
  --modes check trap multi --out work/paired
```

For official-agent guidance, use the same agent configuration and bridge as the baseline with `--agent-config`. Pairing matches issue, revision, agent/model, protocol, access contract, cutoff, baseline-input signature, and weight.

| Output | Description |
|---|---|
| `DeltaSupport`, `FCred`, `DeltaPV` | Signed net changes, Eqs. (7)-(8) |
| `C_FCred`, `C_FCregression` | Favorable/adverse false-credit transitions |
| `C_PVgain`, `C_PVloss` | Invalid-to-valid and valid-to-invalid transitions |
| `TokPerFC` | Net additional tokens across the joint correction/cost cohort divided by favorable FC corrections, Eq. (9) |
| `support-responsive` | At least one completed mode acquires support |
| `persistent` | Every prescribed mode is completed and all have zero support |
| `unresolved` | No support acquisition with incomplete mode coverage |

Intervention tokens/time include baseline resources plus measured additional cost. File-count changes use a common counting definition. Favorable corrections and net changes are reported separately. `TokPerFC` is null when there are no favorable corrections.

## Reference-context and repair experiments

Build source-verified reference routes and run the supplied-context oracle:

```bash
python -m ptrace build-reference --data data --cohort lite \
  --repo-map configs/repo_map.json --out work/verified_evaluator.jsonl

python -m ptrace run --method oracle-chain --data data --cohort lite \
  --truth work/verified_evaluator.jsonl --repo-map configs/repo_map.json \
  --model-label GPT-5.5 --out work/oracle.jsonl
```

The oracle uses `verified_reference_paths` and records the `supplied_verified_context_oracle` protocol. Native, supplied-context, and candidate-recognition experiments retain their respective access contracts.

Candidate recognition:

```bash
python -m ptrace run --method candidate-selection --data data --cohort lite \
  --model-label DeepSeek-v3.2 --out work/candidate_selection.jsonl
```

Candidate IDs are randomized; the evaluator records the selection outcome.

Export patches generated by an agent run for the official Python SWE-bench harness:

```bash
python scripts/export_swebench.py --runs work/python_repair_runs.jsonl \
  --out work/swebench_predictions.jsonl

python -m swebench.harness.run_evaluation \
  --dataset_name SWE-bench/SWE-bench_Verified \
  --predictions_path work/swebench_predictions.jsonl \
  --max_workers 1 --run_id ptrace_validation
```

Use the source dataset/split, matching pre-fix commit, test patch, and FAIL_TO_PASS/PASS_TO_PASS requirements. Pro Public tasks use their benchmark's official language-specific environments. `construction_inputs.jsonl` stores construction and test requirements; agent-generated patches are taken from `model_patch` in run records.

Agreement on binary annotation fields `reference` and `annotation`:

```bash
python -m ptrace agreement --annotations <annotation_JSONL> \
  --out work/agreement.json
```

This reports agreement and Cohen's kappa over non-null binary judgments.

## Paper tables and Figure 3

Experiment scopes in `configs/experiments.json`: Tables 1/4/8 use Full; Tables 2/5/6/7/9/10/11/12 and Figure 3 use Lite.

```bash
python -m ptrace tables --data data --scores work/all_scores.jsonl \
  --out work/paper_tables

python scripts/plot_profiles.py --data work/paper_tables/lite/strata.csv \
  --out work/figures
```

Merge non-overlapping score records before exporting tables. Category-specific guidance variants come from `category_variants` in the experiment configuration.

Figure 3 has five agent sectors, six category and three hop ticks per sector, and three panels: File@5/HNegOv/TV, Detour/Recovery, and Tokens/Time. Resource coordinates use `1 - Tokens/7300` and `1 - Time/310`; other rates use `value/100`, and Detour uses `1 - value/100`. Outputs are PDF, SVG, and PNG. `--allow-partial` displays gaps for unavailable profiles.

## Release checks and license

```bash
python -m ptrace privacy-check --root .
```

Configure credentials locally through environment variables. Public repository identifiers and source issue IDs provide dataset provenance. `SHA256SUMS` lists checksums for the release files.

New code is licensed under MIT; see `LICENSE_CODE`. Third-party datasets, issue text, patches, repository content, and agent implementations retain their respective licenses.

## Complete experiment

```bash
python scripts/prepare_repos.py --cohort lite
python scripts/run_experiment.py --method ptrace --cohort lite \
  --repo-map configs/repo_map.json --model-label GPT-5.5 --out work/experiment
```

The runner implements Algorithm 1. Each mode starts from its own baseline copy; evaluator targets remain in the audit stage. `--baseline` reuses canonical runs and `--reviews` loads independent source and trace audits. Generated output includes paired changes, support timelines, response labels, and tables.

## Observation mapping and context removal

```bash
python -m ptrace build-graph --cohort lite --limit 5 \
  --repo-map configs/repo_map.json --out work/graphs
python -m ptrace map-observations --cohort lite --runs work/runs.jsonl \
  --repo-map configs/repo_map.json --out work/mapped
python -m ptrace evaluate --cohort lite --runs work/mapped/runs.jsonl \
  --repo-map configs/repo_map.json --reviews work/mapped/reviews.jsonl --out work/mapped_eval
python -m ptrace ablate-context --cohort lite --runs work/mapped/runs.jsonl \
  --repo-map configs/repo_map.json --reviews work/mapped/reviews.jsonl --out work/ablation.jsonl
```

The source graph resolves Python declarations, import aliases, relative imports, plain class methods, and direct test calls. `map-observations` admits only relations whose anchors occur in recorded spans. Context removal re-audits complete alternatives after removing each observation.

## Exact runtime prompts

`docs/PROMPTS.md` contains the complete templates used at runtime. Each mode uses its template through `ptrace.prompts.load_prompt()`. The package includes a checksum index at `configs/prompt_checksums.json`.

Same-agent guidance inherits the baseline retrieval contract, including revision, allowed tools, search limit, and step budget. Public projections remove evaluator annotations from task, route, and observation input objects.

Auxiliary verifier reranking uses its own model settings and `configs/verifier.json`:

```bash
python -m ptrace rerank --cohort lite --baseline work/experiment/baselines.jsonl \
  --verifier-config configs/verifier.json --model-label VERIFIER_NAME --out work/verifier.jsonl
python -m ptrace evaluate --cohort lite --runs work/verifier.jsonl \
  --repo-map configs/repo_map.json --auto-audit --out work/verifier_eval
```

The verifier receives baseline candidate proposals and admitted source spans. Its configured ratings reorder and threshold candidates. Independent source audits determine PV, TV, Support, and FC. Cumulative resource totals and verifier-only token/time increments are both retained.
