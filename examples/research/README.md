# Retrieval research handoff

Compare binary term overlap and BM25 on a fixed miniature document collection.
This is a context-tool acceptance fixture, not a benchmark for model capability.

The producer evaluates the development queries, reports configuration, metrics,
limitations and an unfinished holdout check. A separate reviewer recovers the
evidence, repeats the development evaluation and evaluates held-out queries.

Commands:

```sh
python3 research.py --split dev --output results/dev.json
python3 research.py --split dev --verify results/dev.json
python3 research.py --split holdout --output results/holdout.json
```

Keep methods, collection and query splits fixed. Report Recall@1 and MRR@3,
per-query rankings and collection/configuration digests. A tiny collection cannot
establish general superiority. A process exit code alone is not a research finding.
