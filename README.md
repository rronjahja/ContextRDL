# Experiment Suite v2: Setup and Run Instructions

This directory contains the revised experiment suite for evaluating Context-RDL. The suite updates the previous evaluation scripts, adds additional experiments, integrates pySHACL-based admissibility checking, and includes an incremental resolver for improved scalability.

## Files

### Drop-in replacement

* `admissibility.py`: Replaces the original admissibility module. It preserves the original `check_admissibility(graph, shapes_path)` signature so that `resolver.py` works unchanged. It adds:

  * `check_admissibility_shacl`: SHACL-based validation using pySHACL.
  * `check_admissibility_incremental`: Zone-scoped incremental validation for faster execution.

The admissibility regime is selected through the environment variable:

```bash
ADMISSIBILITY_REGIME=incremental|shacl
```

The default regime is `incremental`.

### New modules

* `resolver_incremental.py`: Provides `resolve_actions_incremental(...)` with the same signature and return tuple as `resolver.resolve_actions`. It performs in-place graph mutation, undo on rejection, and zone-scoped admissibility checking.
* `replay_full.py`: Provides `replay_full(trace_path=...)`. It re-evaluates rules on recorded events, regenerates the schedule, re-runs the resolver, and compares every step against the recorded trace.

### Regression tests

These tests should pass before running or reporting the experiment results:

* `test_validator_equivalence.py`: Checks whether the pySHACL validator and the incremental validator agree on single and pairwise perturbations.
* `test_resolver_equivalence.py`: Checks whether the incremental resolver produces the same accepted action IDs, decision keys, and successor digest as the original resolver across six workloads.

### Experiments

* `experiment_pyshacl_baseline.py`: Compares the proposed approach against a pySHACL-gated baseline and a post-hoc random baseline.
* `experiment_determinism_stress.py`: Evaluates deterministic scheduling under tied actions that write to the same target.
* `experiment_scalability_v2.py`: Compares the original resolver and the incremental resolver across workloads from N=10 to N=800.
* `experiment_governance_v2.py`: Evaluates admissibility and shadowing behavior under different precedence settings.
* `experiment_trace_integrity.py`: Performs full-pipeline replay of the recorded trace and a fresh round-trip trace.

### Orchestration

* `run_all_v2.py`: Runs the regression tests first. If they pass, it runs all five experiments and writes the consolidated output to:

```bash
results/experiments_v2_summary.json
```

## Setup

Install the required dependencies:

```bash
pip install -r requirements.txt
```

The files are expected to be located at the project root alongside the existing modules, including:

```text
resolver.py
rule_engine.py
engine.py
state_transition.py
trace.py
dataset_builder.py
experiment_helpers.py
rule_loader.py
```

Expected data layout:

```text
./shapes/base_graph.ttl
./shapes/invariants.ttl
./configs/settings.json
./configs/rules.json
./data/events.jsonl
./data/contexts.json
./results/
```

The `results/` directory is created automatically if it does not already exist.

`admissibility.py` is a drop-in replacement and should overwrite the existing file. All other files are additions.

## Running the Full Experiment Suite

Run the orchestrator:

```bash
python run_all_v2.py
```

The orchestrator performs the following steps:

1. Runs `test_validator_equivalence.py`.
2. Runs `test_resolver_equivalence.py`.
3. Runs all five experiments in sequence.
4. Writes the consolidated summary to `results/experiments_v2_summary.json`.

Each experiment can also be run individually:

```bash
python test_validator_equivalence.py
python test_resolver_equivalence.py
python experiment_pyshacl_baseline.py
python experiment_determinism_stress.py
python experiment_scalability_v2.py
python experiment_governance_v2.py
python experiment_trace_integrity.py
```

## Running with Full pySHACL Validation

By default, `resolver.py` calls `check_admissibility`, which dispatches to the incremental admissibility checker. To force full pySHACL validation inside the main pipeline, run:

```bash
ADMISSIBILITY_REGIME=shacl python engine.py
```

This mode is slower than the incremental regime but should produce the same outcome when both validators agree. This equivalence is checked by `test_validator_equivalence.py`.

## Experiment Contributions

| Experiment                      | Purpose                   | Contribution                                                                                  |
| ------------------------------- | ------------------------- | --------------------------------------------------------------------------------------------- |
| `test_validator_equivalence`    | Validator regression test | Checks whether the incremental validator is consistent with the SHACL constraints.            |
| `test_resolver_equivalence`     | Resolver regression test  | Checks whether the incremental resolver preserves the original resolver semantics.            |
| `experiment_pyshacl_baseline`   | Baseline comparison       | Compares the proposed approach with pySHACL-gated and post-hoc random baselines.              |
| `experiment_determinism_stress` | Determinism stress test   | Evaluates schedule stability when multiple tied actions target the same resource.             |
| `experiment_scalability_v2`     | Scalability experiment    | Compares the original resolver and the incremental resolver across increasing workload sizes. |
| `experiment_governance_v2`      | Governance scenario       | Evaluates admissibility rejection and shadowing behavior under different precedence settings. |
| `experiment_trace_integrity`    | Replayability experiment  | Performs full-pipeline replay and compares decision-level execution traces.                   |

## Troubleshooting

### pySHACL is not installed

Install pySHACL manually:

```bash
pip install pyshacl
```

### Validator equivalence test fails

Check the installed pySHACL version:

```bash
pip show pyshacl
```

Also verify that SPARQL constraints are enabled through `advanced=True`.

### Resolver equivalence test fails

Inspect the generated comparison file:

```bash
results/test_resolver_equivalence.json
```

This file contains the difference between the original resolver and the incremental resolver decision lists.

### Replay digest matches but decisions differ

If `digest_match=True` but `decisions_match=False`, the execution reached the same final state through a different decision path. Inspect:

```bash
results/experiment_trace_integrity.json
```

This file identifies the step at which the replay diverged.