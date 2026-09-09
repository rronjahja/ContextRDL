# ContextRDL: reproducible execution semantics for rule-based automation over evolving RDF graphs

Reference implementation, workloads, shape graphs, recorded traces and experiment harness for the manuscript
*Reproducible Execution Semantics for Rule-Based Automation over Evolving Knowledge Graphs* (Jahja and Stankovski).

One evaluation step maps `(G_t, W_t, R, C_t)` to `(G_{t+1}, T_t)`: enabled rule instances become action instances,
actions are ordered by the six-part scheduling key `(roleRank, priority, tsKey, rid, bindKey, aid)`, each action passes
four gates in the normative order (role filter, policy guard, first-writer-wins conflict gate, admissibility against the
SHACL shape graph), accepted actions are committed in schedule order, and the step is recorded as a replayable trace.

## Layout

```text
configs/settings.json      execution configuration (window, schedule key, role precedence, conflict policy,
                           enforce_active_roles, event_delivery)
configs/rules.json         the eight HVAC rules r1..r8 (JSON records with a SPARQL SELECT condition)
data/base_graph.ttl        default state G_t (ZoneA/ZoneB/ZoneC + policy node)
data/events.jsonl          default event window (six events)
data/contexts.json         role contexts: default, occupant-inactive, operator-inactive
shapes/invariants.ttl      the SHACL shape graph (six shapes)
src/                       engine, verification suites and experiment scripts (see table below)
src/paths.py               the one place that defines where results are written
ev/                        second application scenario (EV-charging cluster): data, shapes, harness
results/hvac/              result files of the building-automation scenario (every table)
results/hvac/traces/       recorded traces of that scenario
results/ev/                result file of the EV-charging scenario
results/ev/traces/         recorded traces of that scenario
requirements.txt           Python dependencies
```

## Setup (Windows PowerShell)

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:PYTHONDONTWRITEBYTECODE = "1"
```

The manuscript's numbers were produced with CPython 3.14, rdflib 7.6.0 and pySHACL 0.31.0 on a 12th Gen Intel
Core i9-12900KF. All semantic results (accepted sets, decisions, successor digests, agreement counts) are
environment-independent; only the runtime columns depend on the machine.

Every script writes below `results\` (see `src\paths.py`) whether it is started from the repository root or from
`src\`; the commands below assume the repository root. Run one evaluation step on the default workload and replay
its trace:

```powershell
python src\engine.py
python src\replay_full.py results\hvac\traces\trace.json
```

The successor digest of the default workload is `4bbb205b597fc0b5310d6f4b1338347fc014cccf8de131cbd63e87bfb563014f`.

## Verification suite

`src\test_review_regressions.py` (26 checks: default digest preserved, duplicate rule identifiers rejected,
multi-line literal round trip, rule-list order does not change the trace, inactive-role actions receive a recorded
decision, RDF term identity in enumerations, non-finite setpoints, changed shape graphs fall back to the reference
validator, empty windows keep distinct identities, no-op writes have empty deltas, replay rejects a corrupted
decision record, exact decimal bounds, node-targeted shapes without a class assertion, shape files changed in place, inline rule sets, replay environment restoration and profile validation, corrupted snapshots and action records, broken persistence chains, exact ledger identifiers) runs together with the three equivalence suites. `src\test_v5_regressions.py` adds 13 tests covering literal-profile fallbacks, actual rule outcomes, backend selection, write preservation, invalid initial graphs, reference numeric errors, and EV process exit codes. Enforcing entry points (non-zero exit status when a check fails): these five suites and `replay_full.py`,
`experiment_hvac_v3.py`, `experiment_role_filter.py`, `experiment_hashseed.py`,
`experiment_governance_v2.py`, `experiment_invalid_start.py`, `experiment_determinism_stress.py`,
`experiment_multiwindow.py`, `experiment_replay_extra.py`, `experiment_cross_implementation.py`,
`experiment_atomic_commit.py`, `experiment_restart_delivery.py`, `experiment_scalability_v2.py`,
`experiment_scalability_sd.py`, and `ev\experiment_ev.py`. `experiment_runtime_controlled.py` and
`experiment_scalability_profile.py` are measurement-only and enforce nothing.

```powershell
python src\test_review_regressions.py
python src\test_v5_regressions.py
python src\test_validator_equivalence.py
python src\test_resolver_equivalence.py
python src\test_validator_differential.py
```

For correctness runs using the reference validator, set `$env:ADMISSIBILITY_REGIME = "shacl"`
before running the resolver and workload checks. Both resolvers honor this setting. Use
`$env:ADMISSIBILITY_REGIME = "incremental"` for the guarded fast path. The validator comparison
suites explicitly invoke both implementations; the differential report separates fast-kernel
comparisons from reference fallbacks. A fallback is not an independent validator comparison.

## Which script produces which table

Run every script from the repository root. Each writes the result file named in the third column.

| Manuscript item | Script | Result file |
| --- | --- | --- |
| Table 3 (walkthrough), Sec. V-B | `src\engine.py` | `results\hvac\traces\trace.json` |
| Table 5 rows 1-2 (validator agreement) | `src\test_validator_equivalence.py` | `results\hvac\test_validator_equivalence.json` |
| Table 5 rows 3-5 (differential campaign) | `src\test_validator_differential.py` | `results\hvac\test_validator_differential.json` |
| Table 6 (four strategies, three workloads) | `src\experiment_hvac_v3.py` | `results\hvac\experiment_hvac_v3.json` |
| Table 7 (controlled 2x2 runtime) | `src\experiment_runtime_controlled.py` | `results\hvac\experiment_runtime_controlled.json` |
| Table 8 (determinism under ties) | `src\experiment_determinism_stress.py` | `results\hvac\experiment_determinism_stress.json` |
| Sec. V-F (cross-process hash seeds) | `src\experiment_hashseed.py` | `results\hvac\experiment_hashseed.json` |
| Table 9 (governance-clean workload) | `src\experiment_governance_v2.py` | `results\hvac\experiment_governance_v2.json` |
| Sec. IV-C / V-G (role filter, gate (i)) | `src\experiment_role_filter.py` | `results\hvac\experiment_role_filter.json`, `results\hvac\traces\trace_role_*.json` |
| Sec. V-H (invalid input graphs) | `src\experiment_invalid_start.py` | `results\hvac\experiment_invalid_start.json` |
| Table 11, Sec. V-I (EV charging) | `ev\experiment_ev.py` | `results\ev\experiment_ev.json`, `results\ev\traces\trace_ev_*.json` |
| Sec. V-J (multi-step, overlapping windows) | `src\experiment_multiwindow.py` | `results\hvac\experiment_multiwindow.json`, `results\hvac\traces\trace_multiwindow_*.json` |
| Table 12 (scalability, mean +/- sd; reference, incremental with the same decision-level trace profile, incremental without per-action digests) | `src\experiment_scalability_sd.py` | `results\hvac\experiment_scalability_sd.json` |
| Sec. V-K scoping (heap, trace size, cross-target cost) | `src\experiment_scalability_profile.py` | `results\hvac\experiment_scalability_profile.json` |
| Resolver equivalence at every N (single run) | `src\experiment_scalability_v2.py` | `results\hvac\experiment_scalability_v2.json` |
| Resolver equivalence, named workloads | `src\test_resolver_equivalence.py` | `results\hvac\test_resolver_equivalence.json` |
| Table 13 rows 1-4 (replay, HVAC pipeline) | `src\experiment_hvac_v3.py` | `results\hvac\traces\trace_default.json`, `trace_tie_conflict.json`, `trace_governance_op_gt_occ.json`, `trace_governance_occ_gt_op.json` |
| Table 13 rows 5-11 (replay from recorded action instances: governance-clean, stress N=64, four EV windows) | `src\experiment_replay_extra.py` | `results\hvac\experiment_replay_extra.json`, `results\hvac\traces\trace_stress_64.json`, `results\hvac\traces\trace_governance_clean_*.json`, `results\ev\traces\trace_ev_*.json` |
| Sec. V-M (cross-implementation) | `src\experiment_cross_implementation.py` | `results\hvac\experiment_cross_implementation.json` |
| Sec. V-N (crash recovery, seven schedules) | `src\experiment_atomic_commit.py` | `results\hvac\experiment_atomic_commit.json` |
| Sec. III-G/III-H (restart-safe consume-once delivery through the store) | `src\experiment_restart_delivery.py` | `results\hvac\experiment_restart_delivery.json` |

Regenerate everything in order (about ten minutes, dominated by the reference resolver at N=800):

```powershell
python src\engine.py
python src\test_validator_equivalence.py
python src\test_validator_differential.py
python src\test_resolver_equivalence.py
python src\experiment_hvac_v3.py
python src\experiment_runtime_controlled.py
python src\experiment_determinism_stress.py
python src\experiment_hashseed.py
python src\experiment_governance_v2.py
python src\experiment_role_filter.py
python src\experiment_invalid_start.py
python ev\experiment_ev.py
python src\experiment_multiwindow.py
python src\experiment_scalability_v2.py
python src\experiment_scalability_sd.py
python src\experiment_scalability_profile.py
python src\experiment_replay_extra.py
python src\experiment_cross_implementation.py
python src\experiment_atomic_commit.py
python src\experiment_restart_delivery.py
```

## Modules

* `rule_engine.py`: rule evaluation over the windowed dataset, action-instance construction and `schedule_actions`
  (the scheduling key). The canonical binding encoding (identity profile `nt-1`) is a sorted-key JSON object whose
  values are the N-Triples forms of the bound RDF terms (IRIs as `<iri>`, literals with datatype or language tag),
  with the event node variable `?e` excluded (its stable identifier `?eid` is a binding value); the action identifier is
  SHA-256 over `(rid, bindKey, window_id)`; deduplication uses that identity, not the hash. Every pipeline trace embeds
  the canonical (rid-sorted) rule set with its hash and the shape graph's content hash under `settings.dependencies`,
  so replay is self-contained; `replay_full` verifies them, the identity profile, the input and successor snapshots,
  the enabled/scheduled/accepted action records, the rule snapshot and every decision field except the validator's
  free-text report. The semantic trace is everything except the `environment` block (run provenance: library
  versions, source revision, source-manifest fingerprint, where the rules were loaded from).
* `resolver.py`: reference resolver; gates (i)-(iv) in the normative order, graph cloning and full-graph validation
  per action, decision-level digests.
* `resolver_incremental.py`: same semantics, in-place mutation with undo and zone-scoped validation
  when the shipped shape file, input profile, initial admissibility and proposed write permit it;
  otherwise full pySHACL validation.
* `admissibility.py`: `check_admissibility_shacl` (pySHACL, reference validator) and
  `check_admissibility_incremental` (guarded hand-written zone-local checker). The dispatcher `check_admissibility`
  selects between them with the environment variable `ADMISSIBILITY_REGIME` (`incremental`, the default, or `shacl`);
  the requested regime is recorded in every trace as `settings.admissibility_regime` and re-applied by `replay_full`.
  Under `incremental`, unsupported shapes or literal representations select pySHACL automatically.
* `trace.py`: canonical sorted N-Triples serialization, SHA-256 digests, trace construction.
* `replay_full.py`: full-implementation replay from a recorded trace (rebuilds the input graph and events, re-evaluates
  the rules from the canonical rule file, re-schedules, re-runs the resolver under the recorded role context and
  validator regime, and compares enabled instances, schedule, decisions, accepted set and successor digest).
* `independent_resolver.py`: the structurally independent second implementation of scheduling and resolution,
  written from the definitions and sharing no code with the modules above.
* `persistence.py`: write-ahead, pointer-switch commit protocol for `(G_{t+1}, T_t)`; committed steps form a
  parent-linked chain. A submitted (graph, trace) pair whose digests disagree is refused before staging; no commit
  proceeds and recovery deletes nothing while the chain does not verify (pointer/trace parent agreement, cycle guard,
  missing parents, input/successor linkage, top-level and nested digests); a stale input is refused (parent check); a
  step identifier on the chain is refused (duplicate). Single writer; crash atomicity, not power-loss durability.
* `run_controller.py`: one restart-safe step over the store: resumes from `CURRENT`'s state and processed-event
  ledger, excludes ledger identifiers by exact membership (consume-once), and commits the ledger after the step
  inside the same atomic step. At most one committed evaluation step per event identifier across restarts;
  a committed step may accept zero, one, or several actions.
* `experiment_helpers.py`: the tie-conflict, governance-conflict and duplicate-identity event lists shared by all
  experiments (a workload name means the same events everywhere).
* `paths.py`: the results layout (`results/hvac`, `results/hvac/traces`, `results/ev`, `results/ev/traces`); scripts
  never write anywhere else.

Digests use the RDFLib N-Triples serializer (sorted lines, SHA-256), so multi-line literals round-trip. The
incremental validator is used only when the shape graph's content (line endings normalized) is the shipped
`shapes/invariants.ttl`; any other shape graph selects the reference pySHACL validator automatically, and shape
graphs are cached by content, so a file edited in place is re-parsed. The data profile checked by
`admissibility.fast_path_eligible` permits no subclasses of `ex:HVAC_Zone` and requires direct zone typing for every
subject carrying a zone property. Setpoints must be finite, well-formed `xsd:decimal` or `xsd:integer` literals with
a compatible RDFLib numeric value; ventilation modes must be plain literals without language tags; emergency
states must be canonical `xsd:boolean` literals `true` or `false`. Float/double datatypes, noncanonical booleans,
malformed literals and other excluded representations go to pySHACL without normalization by the validator.
If reference numeric comparison raises `decimal.InvalidOperation` (for example on Decimal NaN), the candidate
is refused and the report identifies a reference-validation error; this is not reported as a pySHACL verdict.
Eligibility describes representations, not admissibility: cardinality faults, out-of-range numbers and invalid
plain-string modes can still be rejected by the fast checker.

The cloning resolver selects the backend on each candidate. The incremental resolver checks input eligibility
and full initial admissibility once, then permits focused checks only for writes to the three constrained
properties of existing directly typed zones, with supported literals. Other writes use pySHACL; after accepting
such a write, the resolver keeps full validation for the rest of that resolution. Focused validation touches only
the changed zone, with an initial full-graph pass. The finite differential and regression suites support agreement
on their tested cases; they are not a universal equivalence proof. The formal preservation claim assumes an
admissibility checker implementing the declared constraints. pySHACL is the reference validator, and the fast
checker is an optimization evaluated on the reported workloads.

Reason codes recorded per decision: `admissible`, `inadmissible`, `shadowed_by_prior_accepted_action`,
`policy_role_cap_violation`, `policy_min_violation`, `inactive_role` (the same six classes in the HVAC and EV implementations). The reason-code class is the text before
the first colon; a detail suffix after the colon (for example the offending value) is not part of the vocabulary.
