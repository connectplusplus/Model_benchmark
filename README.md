# Claude Frontier Benchmark

This folder is a reconfigured benchmark for enterprise IT buyers. The original
suite mostly measured whether each model could solve familiar coding exercises.
That made Sonnet look like the obvious choice: it passed the same tasks at lower
cost.

This version changes the buyer question:

> When does the strongest model reduce risk enough to justify the premium?

## What Changed

- The simple task is now only a calibration check.
- Frontier tasks carry most of the score weight.
- Tasks are enterprise-shaped: incident correlation, authorization policy, and
  database migration planning.
- Test harnesses check edge cases that matter in production: determinism, deny
  precedence, zero-downtime migration sequencing, stable IDs, malformed input,
  and audit-style explanations.
- The UI reports a weighted frontier score plus cost per successful trial.

## Files

| File | Role |
|---|---|
| `app.py` | Flask web harness on port 5001. |
| `tasks.py` | Weighted benchmark task definitions and tests. |
| `benchmark.py` | Optional CLI runner using the same task suite. |

## Run

```bash
pip install flask anthropic
python app.py
```

Open:

```text
http://127.0.0.1:5001
```

Paste an Anthropic API key, choose models, set trial count, and run. The key is
kept in memory for the request and is not written to disk.

## How To Interpret Results

Use Sonnet when it ties the frontier score on your task mix. Use Opus when it
materially improves pass rate on weighted frontier tasks. For enterprise buyers,
the premium model is not justified by prettier syntax. It is justified when it
reduces:

- failed change plans
- unsafe authorization logic
- noisy or misleading incident analysis
- migration rollback risk
- senior engineer review cycles

The right chart for this benchmark is not just "cost per task." It is "cost per
successful high-risk task" and "weighted frontier score at a given spend."

## Current Result Diagnosis

The existing CSVs show the original advanced task is not advanced enough. It is a
single-function arithmetic parser with a short public harness. Sonnet can often
solve it, and when it fails the failure is a shortcut guard rather than evidence
that the task required deeper enterprise reasoning. That result tells buyers to
prefer the cheaper model.

This suite is designed to surface a different and more useful distinction:
whether the model can keep many constraints active at once and produce code that
survives production-like edge cases.
