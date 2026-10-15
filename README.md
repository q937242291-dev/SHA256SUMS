# PTRACE-SWE

Trajectory-aware evaluation, source-grounded localization, and tool-assisted diagnosis for software engineering agents.

## Quick start

Python 3.10+ and Git are required. Run commands from this directory.

```bash
python -m pip install -r requirements.txt
python scripts/reproduce.py
```

The offline example validates the dataset and source-span cache, runs the tests, and demonstrates baseline evaluation, three independent guidance modes, and paired metrics. Outputs are written to `work/example`.

## Data

Full contains 1,547 task records (1,454 distinct source/revision tasks), with source-verified references for 755 records. Lite contains 300 source-verified tasks.

| File | Contents |
|---|---|
| `data/issuechain_full.jsonl` | 1,547 task records across 29 repositories |
| `data/issuechain_lite.jsonl` | 300 tasks with source-checked reference routes and strata |
| `data/instances.jsonl` | Agent inputs: issue, repository, and pre-fix commit |
| `data/evaluator.jsonl` | Targets, reference routes, negatives, and source reviews |
| `data/construction_inputs.jsonl` | Accepted patches and required-test definitions |
| `data/source_spans.jsonl.gz` | Commit-pinned source spans and hashes |
| `data/source_manifest.jsonl` | Indexed pre-fix files and Git object hashes |
| `data/lite_ids.json`, `data/splits.json` | Fixed Lite IDs and grouped splits |
| `data/dataset_spec.json` | Counts, categories, hops, and annotation metadata |
| `data/paper_tables/` | Paper-reported table values |

```python
from ptrace.io import read_jsonl, public_instance

task = next(read_jsonl("data/issuechain_lite.jsonl"))
agent_input = public_instance(task)
print(agent_input["instance_id"], agent_input["repo"])
```

## Run and evaluate

Start with five Lite tasks and the API-free BM25 baseline:

```bash
python scripts/prepare_repos.py --cohort lite --limit 5
python -m ptrace run --method bm25 --cohort lite --limit 5 \
  --repo-map configs/repo_map.json --out work/bm25.jsonl
python -m ptrace evaluate --cohort lite --limit 5 --runs work/bm25.jsonl \
  --repo-map configs/repo_map.json --auto-audit --out work/bm25_eval
```

For model-based methods, configure your provider and run source exploration plus the three guidance modes:

```bash
export PTRACE_API_KEY="YOUR_KEY"
export PTRACE_BASE_URL="https://YOUR_PROVIDER/v1"
export PTRACE_MODEL="YOUR_MODEL_ID"
python scripts/run_experiment.py --method ptrace --cohort lite --limit 5 \
  --repo-map configs/repo_map.json --model-label YOUR_MODEL_NAME --out work/experiment
```

Remove `--limit 5` for the full cohort; use `--cohort full` for Full evaluation and repository preparation.

| Methods | Implementation |
|---|---|
| `ptrace`, `reference-native` | Interactive source exploration |
| `reference-staged`, `reference-structured` | File/symbol/line localization and structure-aware search |
| `bm25`, `bm25-rerank`, `long-context`, `oracle-chain` | Retrieval and context baselines |
| `candidate-selection` | Blinded candidate-route recognition |
| `check`, `trap`, `multi` | Generate-then-check, trap-aware, and multi-hop guidance |
| `external` | Agentless, AutoCodeRover, OpenHands, SWE-agent, Augment, Codex, and Claude Code integrations |

Metrics include File@k, MRR(This section is optional and can be omitted as it is not mentioned in the paper), NDCG(This section is optional and can be omitted as it is not mentioned in the paper), PV, TV, Support, FC, HNegOv, EdgeF1, detours, recovery, resource use, paired changes, and TokPerFC. Python, Go, JavaScript, and TypeScript source bindings share the same audit interface.

## Prompts and paper outputs

Exact runtime prompts are in `ptrace/prompt_templates/` and [docs/PROMPTS.md](docs/PROMPTS.md). Same-agent guidance uses the recorded retrieval contract; auxiliary verifier reranking uses `configs/verifier.json`. Gold targets, paths, and labels are evaluator-only.

```bash
python -m ptrace tables --scores work/experiment/scores.jsonl --out work/tables
```

See [docs/USAGE.md](docs/USAGE.md) for official runtimes, interfaces, reranking, and plotting; [docs/METHODS.md](docs/METHODS.md) maps the paper methods and equations to code. Code license: [LICENSE_CODE](LICENSE_CODE).
