# Interval union: first development experiment

2026-11-16. Four independent implementations of a newly authored synthetic Python
task were produced in separate ordinary Chef NoVcs slots. Two were single-agent
runs; two received a second independent agent review. All implementation and review
agents were requested as Codex `gpt-6.1-sol` / `medium`; actual model application and
token/credit counters were not independently available. These are configured labels,
not observed provider billing or runtime identity evidence.

| Strategy | Implementation agents | Independent review agents | Public cases | Sealed holdout |
| --- | --- | --- | --- | --- |
| single-a | 1 | 0 | 26/26 | 106/106 |
| single-b | 1 | 0 | 26/26 | 106/106 |
| reviewed-a | 1 | 1 | 26/26 | 106/106 |
| reviewed-b | 1 | 1 | 26/26 | 106/106 |

Reviewers found zero defects and made no changes. They used their own tests and
independent overlap-graph oracles, without receiving holdout data or sibling code.
Candidate code was frozen before scoring. The harness scored all candidates in
network-isolated, read-only bubblewrap environments; the held-out cases and expected
answers were not filesystem mounts. Candidate code receives inputs for the current
evaluation batch, but cannot read stored holdout files or expected answers.

No quality improvement was observed. Two successful independent runs per kind and
one simple task are insufficient to establish equal general performance or rank the
strategies. The approximately 0.03s holdout batch times mostly measure interpreter
and isolation startup; their tiny differences do not measure development efficiency.
Development time, tokens, price and credits are **unknown**. The additional review
calls did not improve this task's observed pass rate; this does not show reviews
are useless on harder tasks. The routing report says `insufficient_evidence` and
recommends retaining the baseline.

This live experiment has **one generation**, four independent implementations and
two reviews. Five-generation/20-artifact orchestration is covered by deterministic
test scorers; it is not a five-generation live-model study. No reset tickets were
used, and credit consumption was not measured. The public example contains only
the synthetic spec, source artifacts, configuration, released seed and metadata
reports; it contains no conversations or real project data.

`report.json` records scores, hashes, selections, evaluator identity and limitations.
`routing.json` records the explicit `single-a` baseline comparison.
`released-holdout.json` releases the saved seed **after** final selection. Reusing
that seed is replay, not another unseen evaluation.

Reproduce the exact scores on Linux with Python and bubblewrap installed:

```bash
python plugins/chef/skills/mission/scripts/lab.py --root examples/lab/interval-union replay
```

The command verifies candidate and generated holdout hashes, evaluates the published
artifacts in isolation and compares scores; it does not call models or write files.
Exact elapsed times will vary. A fresh experiment uses `config.json` with a new
nonexistent `--root` and `prepare`; workers produce `solution.py` in each generated
slot, and `evaluate` scores them. New hidden seeds are generated outside slots.
