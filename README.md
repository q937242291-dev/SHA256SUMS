# PTRACE-SWE

Includes IssueChainBench **Full (1,547 tasks)** and **Lite (300 tasks)**, baselines, three guidance modes, and paper metrics.

## Quick start

Requires **Python 3.10+** and **Git**. Run from this directory; the core needs no Python dependencies.

```bash
python scripts/reproduce.py
```

This validates the dataset, runs the tests, and executes an offline example of source auditing, independent guidance, paired evaluation, and table generation. Outputs go to `work/example`. You can run the example again with the same command.

## Dataset

| File | Use |
|---|---|
| `data/issuechain_full.jsonl` | Full records: 1,547 tasks across 29 repositories |
| `data/issuechain_lite.jsonl` | Fixed 300-task Lite subset |
| `data/instances.jsonl` | Agent inputs: issue, repository, pre-fix commit |
| `data/evaluator.jsonl` | Targets, routes, negatives, evaluation metadata |
| `data/construction_inputs.jsonl` | Patches and test specifications |
| `data/lite_ids.json`, `data/splits.json` | Lite IDs and supervised splits |

```python
from ptrace.io import read_jsonl, public_instance

for task in read_jsonl("data/issuechain_lite.jsonl"):
    agent_input = public_instance(task)
    print(agent_input["instance_id"], agent_input["repo"])
```

## Run

Prepare the selected pre-fix repositories and run the API-free BM25 baseline:

```bash
python scripts/prepare_repos.py --cohort lite
python -m ptrace run --method bm25 --cohort lite \
  --repo-map configs/repo_map.json --out work/bm25.jsonl
python -m ptrace evaluate --cohort lite --runs work/bm25.jsonl \
  --repo-map configs/repo_map.json --out work/bm25_eval
```

For model-based methods, set your provider settings in the terminal:

```bash
export PTRACE_API_KEY="YOUR_KEY"
export PTRACE_BASE_URL="https://YOUR_PROVIDER/v1"
export PTRACE_MODEL="YOUR_MODEL_ID"
```

Run source exploration plus all three independent guidance modes:

```bash
python scripts/run_experiment.py --method ptrace --cohort lite \
  --repo-map configs/repo_map.json --model-label YOUR_MODEL_NAME \
  --out work/experiment
```

Use `--cohort full` for Full and `--limit 5` for a small run. Use the same cohort when preparing repositories. Anthropic endpoints use `--provider anthropic`.

| Method / mode | Implementation |
|---|---|
| `ptrace`, `reference-native` | Interactive search, reads, symbols, source relations |
| `reference-staged` | File → symbol → line localization |
| `reference-structured` | Declaration retrieval and relation exploration |
| `bm25`, `bm25-rerank` | Lexical retrieval; optional model reranking |
| `long-context`, `oracle-chain` | Source-context and verified-chain baselines |
| `candidate-selection` | Blinded candidate-chain recognition |
| `check`, `trap`, `multi` | Generate-then-check, trap-aware, multi-hop guidance |
| `external` | Official-agent runtime integration |

Official integrations cover Agentless, AutoCodeRover, OpenHands, SWE-agent, Augment, Codex, and Claude Code. Runtime setup and the JSON bridge interface are in [docs/USAGE.md](docs/USAGE.md).

## Prompts

The complete prompts loaded by the code are in `ptrace/prompt_templates/` and [docs/PROMPTS.md](docs/PROMPTS.md). Same-agent guidance reuses the recorded retrieval contract. Auxiliary verifier reranking uses `configs/verifier.json`:

```bash
python -m ptrace rerank --cohort lite --baseline work/experiment/baselines.jsonl \
  --verifier-config configs/verifier.json --model-label VERIFIER_NAME --out work/verifier.jsonl
```

## Evaluate

```bash
python -m ptrace evaluate --cohort lite --runs work/runs.jsonl \
  --repo-map configs/repo_map.json --auto-audit --out work/evaluation
python -m ptrace tables --scores work/evaluation/scores.jsonl --out work/tables
```

`--auto-audit` resolves Python declarations, imports, and direct calls. Other semantic relation types use source reviews supplied with `--reviews`. The experiment runner additionally maps source observations to complete alternative routes.

Metrics include File@k, MRR, NDCG, FileF1, PV, TV, Support, FC, HNegOv, EdgeF1, detours, recovery, paired changes, correction counts, and TokPerFC. Each aggregate includes its eligible count. Experiment outputs include summaries, scores, paired changes, support timelines, response labels, and CSV tables.

Optional Figure 3 export:

```bash
python -m pip install -r requirements-figures.txt
python scripts/plot_profiles.py --data work/tables/lite/strata.csv --out work/figures
```

See [docs/USAGE.md](docs/USAGE.md) for interfaces and additional commands, and [docs/METHODS.md](docs/METHODS.md) for the paper-to-code map. Code is released under [LICENSE_CODE](LICENSE_CODE).
