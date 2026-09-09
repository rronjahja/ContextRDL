"""Regressions for the v4 audit: literal profile, backend selection and EV exits.

Run from the repository root: python src/test_v5_regressions.py
Literal counterexamples preserve lexical forms without disabling global
RDFLib normalization. EV checks inject failed results into the actual main().
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from rdflib import Graph, Literal, Namespace
from rdflib.namespace import XSD

import admissibility as adm
import resolver_incremental as inc
from dataset_builder import build_dataset, load_state
from resolver import resolve_actions
from rule_engine import evaluate_rules, load_settings, resolve_governance_context, schedule_actions
from rule_loader import load_rules
from trace import graph_digest

ROOT = Path(__file__).resolve().parent.parent
EX = Namespace("http://example.org/building#")


def preheat_schedule(graph):
    events = [{"eid": "v5-preheat", "type": "PreheatRequest", "role": "operator",
               "timestamp": "2026-03-14T09:00:00Z",
               "payload": {"zone": str(EX.ZoneB), "target": 22}}]
    settings = load_settings()
    context = resolve_governance_context(settings=settings, contexts_path="data/contexts.json")
    dataset, meta = build_dataset(graph, events, settings=settings)
    enabled = evaluate_rules(dataset, load_rules("configs/rules.json"), settings=settings,
                             context=context, window_meta=meta)
    return schedule_actions(enabled, settings=settings)


def decision_projection(decisions):
    return [{k: v for k, v in d.items() if k != "validation_report"} for d in decisions]


class ValidatorProfileTests(unittest.TestCase):
    def setUp(self):
        self.state = load_state("data/base_graph.ttl")
        self.env = patch.dict(os.environ, {"ADMISSIBILITY_REGIME": "incremental"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_preserved_literals_use_reference_and_agree(self):
        cases = [
            (EX.emergencyState, Literal("1", datatype=XSD.boolean, normalize=False), True),
            (EX.emergencyState, Literal("0", datatype=XSD.boolean, normalize=False), True),
            (EX.emergencyState, Literal("not_boolean", datatype=XSD.boolean, normalize=False), False),
            (EX.currentSetpoint, Literal("26.0000000000000001", datatype=XSD.double, normalize=False), True),
            (EX.currentSetpoint, Literal("17.9999999999999999", datatype=XSD.double, normalize=False), True),
            (EX.currentSetpoint, Literal("22", datatype=XSD.float, normalize=False), True),
            (EX.ventilationMode, Literal("normal", lang="en"), False),
        ]
        for predicate, value, expected in cases:
            with self.subTest(literal=value.n3()):
                graph = load_state("data/base_graph.ttl")
                graph.set((EX.ZoneB, predicate, value))
                before = graph_digest(graph)
                self.assertFalse(adm.fast_path_eligible(graph)[0])
                self.assertEqual(adm.check_admissibility_shacl(graph)[0], expected)
                with patch.object(adm, "_check_admissibility_fast", wraps=adm._check_admissibility_fast) as fast:
                    with patch.object(adm, "check_admissibility_shacl", wraps=adm.check_admissibility_shacl) as ref:
                        self.assertEqual(adm.check_admissibility(graph)[0], expected)
                        self.assertEqual(adm.check_admissibility_incremental(graph)[0], expected)
                        self.assertEqual(fast.call_count, 0)
                        self.assertEqual(ref.call_count, 2)
                self.assertEqual(graph_digest(graph), before)

    def test_supported_boundaries_are_independent_fast_comparisons(self):
        cases = [
            (EX.ZoneB, "18", None, True),
            (EX.ZoneB, "26", None, True),
            (EX.ZoneB, "17.9999999999999999999999", None, False),
            (EX.ZoneB, "26.0000000000000000000001", None, False),
            (EX.ZoneA, "23", None, True),
            (EX.ZoneA, "23.0000000000000000000001", None, False),
            (EX.ZoneA, "21", "off", True),
            (EX.ZoneA, "21.0000000000000000000001", "off", False),
        ]
        for zone, lexical, ventilation, expected in cases:
            with self.subTest(zone=zone, lexical=lexical, ventilation=ventilation):
                graph = load_state("data/base_graph.ttl")
                graph.set((zone, EX.currentSetpoint, Literal(lexical, datatype=XSD.decimal, normalize=False)))
                if ventilation is not None:
                    graph.set((zone, EX.ventilationMode, Literal(ventilation)))
                self.assertTrue(adm.fast_path_eligible(graph)[0])
                self.assertEqual(adm.check_admissibility_shacl(graph)[0], expected)
                with patch.object(adm, "check_admissibility_shacl", side_effect=AssertionError("unexpected fallback")):
                    self.assertEqual(adm.check_admissibility_incremental(graph)[0], expected)

    def test_reference_numeric_error_refuses_candidate(self):
        from decimal import Decimal
        self.state.set((EX.ZoneB, EX.currentSetpoint, Literal(Decimal("NaN"), datatype=XSD.decimal)))
        self.assertFalse(adm.fast_path_eligible(self.state)[0])
        conforms, report = adm.check_admissibility_shacl(self.state)
        self.assertFalse(conforms)
        self.assertIn("Reference validation error", report)
        self.assertFalse(adm.check_admissibility_incremental(self.state)[0])

    def test_actual_r7_counterexample_agrees_in_both_resolvers_and_regimes(self):
        graph = self.state
        graph.set((EX.ZoneB, EX.emergencyState, Literal("1", datatype=XSD.boolean, normalize=False)))
        schedule = preheat_schedule(graph)
        self.assertEqual([a["rid"] for a in schedule], ["r7"])
        before = graph_digest(graph)
        reference_projection = None
        for regime in ("shacl", "incremental"):
            with patch.dict(os.environ, {"ADMISSIBILITY_REGIME": regime}):
                for resolver, kwargs in ((resolve_actions, {}),
                                         (inc.resolve_actions_incremental, {"record_digests": True})):
                    with self.subTest(regime=regime, resolver=resolver.__name__):
                        accepted, successor, decisions = resolver(graph, schedule, **kwargs)
                        self.assertEqual([a["rid"] for a in accepted], ["r7"])
                        self.assertEqual(float(successor.value(EX.ZoneB, EX.currentSetpoint)), 22.0)
                        self.assertTrue(adm.check_admissibility_shacl(successor)[0])
                        projection = (graph_digest(successor), decision_projection(decisions))
                        if reference_projection is None:
                            reference_projection = projection
                        self.assertEqual(projection, reference_projection)
                        self.assertEqual(graph_digest(graph), before)

    def test_requested_shacl_never_calls_fast_kernel(self):
        schedule = preheat_schedule(self.state)
        for regime in ("shacl", "SHACL"):
            with patch.dict(os.environ, {"ADMISSIBILITY_REGIME": regime}):
                with patch.object(adm, "_check_admissibility_fast", side_effect=AssertionError("fast kernel used")):
                    with patch.object(inc, "_check_admissibility_fast", side_effect=AssertionError("fast kernel used")):
                        for resolver in (resolve_actions, inc.resolve_actions_incremental):
                            with self.subTest(regime=regime, resolver=resolver.__name__):
                                self.assertEqual(len(resolver(self.state, schedule)[0]), 1)

    def test_unknown_regime_rejected_including_empty_schedule(self):
        with patch.dict(os.environ, {"ADMISSIBILITY_REGIME": "typo"}):
            for resolver in (resolve_actions, inc.resolve_actions_incremental):
                for schedule in ([], preheat_schedule(self.state)):
                    with self.subTest(resolver=resolver.__name__, empty=not schedule):
                        with self.assertRaisesRegex(ValueError, "Unknown admissibility regime"):
                            resolver(self.state, schedule)

    def test_supported_updates_retain_focused_fast_path(self):
        schedule = preheat_schedule(self.state)
        with patch.object(inc, "_check_admissibility_fast", wraps=inc._check_admissibility_fast) as fast:
            with patch.object(inc, "check_admissibility_shacl", side_effect=AssertionError("unexpected fallback")):
                accepted, _, _ = inc.resolve_actions_incremental(self.state, schedule)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(fast.call_count, 2)  # One full initial check, one touched-zone check.
        self.assertEqual(fast.call_args.kwargs["focus_zones"], {EX.ZoneB})

    def test_invalid_untouched_zone_requires_global_validation(self):
        self.state.set((EX.ZoneC, EX.ventilationMode, Literal("turbo")))
        schedule = preheat_schedule(self.state)
        self.assertTrue(adm.fast_path_eligible(self.state)[0])
        for resolver in (resolve_actions, inc.resolve_actions_incremental):
            with self.subTest(resolver=resolver.__name__):
                accepted, _, decisions = resolver(self.state, schedule)
                self.assertEqual(accepted, [])
                self.assertEqual(decisions[0]["reason"], "inadmissible")

    def test_accepted_write_outside_profile_keeps_later_validation_global(self):
        regular = preheat_schedule(self.state)[0]
        outside = dict(regular, aid="outside-profile", rid="constructed-outside-profile",
                       zone=str(EX.NewUntypedZone), predicate=str(EX.ventilationMode), value="turbo",
                       target_key=f"{EX.NewUntypedZone}|{EX.ventilationMode}")
        schedule = [outside, regular]
        self.assertTrue(adm.check_admissibility_shacl(self.state)[0])
        original = resolve_actions(self.state, schedule)
        with patch.object(inc, "check_admissibility_shacl", wraps=inc.check_admissibility_shacl) as ref:
            with patch.object(inc, "_check_admissibility_fast", wraps=inc._check_admissibility_fast) as fast:
                incremental = inc.resolve_actions_incremental(self.state, schedule, record_digests=True)
        self.assertEqual(len(incremental[0]), 2)
        self.assertEqual(ref.call_count, 2)
        self.assertEqual(fast.call_count, 1)  # Initial graph only.
        self.assertFalse(adm.fast_path_eligible(incremental[1])[0])
        self.assertEqual(graph_digest(original[1]), graph_digest(incremental[1]))
        self.assertEqual(decision_projection(original[2]), decision_projection(incremental[2]))


def ev_gate_subprocess(case):
    spec = importlib.util.spec_from_file_location("v5_ev_experiment", ROOT / "ev/experiment_ev.py")
    ev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ev)
    replay_checks = ("dependencies_match", "schedule_match", "decisions_match",
                     "accepted_match", "digest_match", "cross_process_digest_match")
    replay = [dict(workload=f"EV charging ({name})", decisions_compared=1,
                   **{key: True for key in replay_checks})
              for name in ("headline", "emergency", "governance_grid_over_fleet", "governance_fleet_over_grid")]
    headline = {"enabled_count": 1, "schedule_rids": [], "decisions": [], "committed_powers": {},
                "final_admissible": True, "successor_digest": "0" * 64}
    emergency = {"schedule": [], "decisions": [], "committed_powers": {},
                 "committed_feeder_state": {}, "final_admissible": True}
    governance = {name: {"committed_power_CP1": 10, "accepted_rids": [], "final_admissible": True}
                  for name in ("grid_over_fleet", "fleet_over_grid")}
    stress = {"runs": 30, "ours_unique_states": 1, "ours_admissible_pct": 100.0,
              "ours_mean_runtime_ms": 0.0, "ours_sd_runtime_ms": 0.0,
              "deterministic_unique_states": 1, "deterministic_admissible_pct": 0.0,
              "random_unique_states": 2, "random_admissible_pct": 0.0}
    if case in replay_checks:
        replay[0][case] = False
    elif case == "missing_check":
        del replay[0]["dependencies_match"]
    elif case == "missing_window":
        replay.pop()
    elif case == "duplicate_window":
        replay[-1] = dict(replay[0])
    elif case == "inadmissible_final":
        headline["final_admissible"] = False
    elif case == "unstable_trials":
        stress["ours_unique_states"] = 2
    elif case != "pass":
        raise ValueError(case)
    with tempfile.TemporaryDirectory() as tmp:
        # Results are written where src/paths.py points; redirect the EV
        # folders to the temporary directory so the gate test never touches
        # results/ev of the repository.
        import paths as layout
        tmp_ev, tmp_traces = Path(tmp) / "results" / "ev", Path(tmp) / "results" / "ev" / "traces"
        with patch.object(layout, "EV", tmp_ev), patch.object(layout, "EV_TRACES", tmp_traces), \
                patch.object(sys, "argv", ["experiment_ev.py"]):
            with patch.multiple(ev, run_headline=lambda: headline, run_emergency=lambda: emergency,
                                run_governance=lambda: governance, run_headline_30_vs_random=lambda: stress,
                                run_replay_all=lambda: replay):
                try:
                    ev.main()
                finally:
                    result = json.loads((tmp_ev / "experiment_ev.json").read_text())
                    print("GATE_RESULT=" + json.dumps(result["verification"]))


class EVExitTests(unittest.TestCase):
    def run_case(self, case, expected_exit):
        proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--ev-gate-case", case],
                              cwd=ROOT, env=os.environ.copy(), capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, expected_exit, proc.stdout + proc.stderr)
        line = next(line for line in proc.stdout.splitlines() if line.startswith("GATE_RESULT="))
        result = json.loads(line.split("=", 1)[1])
        self.assertEqual(result["overall_pass"], expected_exit == 0)
        if expected_exit:
            self.assertIn("EV verification failed:", proc.stderr)
            self.assertTrue(result["failures"])

    def test_successful_results_exit_zero(self):
        self.run_case("pass", 0)

    def test_each_failed_replay_axis_exits_nonzero(self):
        for case in ("dependencies_match", "schedule_match", "decisions_match", "accepted_match",
                     "digest_match", "cross_process_digest_match"):
            with self.subTest(case=case):
                self.run_case(case, 1)

    def test_missing_or_duplicate_replay_evidence_exits_nonzero(self):
        for case in ("missing_check", "missing_window", "duplicate_window"):
            with self.subTest(case=case):
                self.run_case(case, 1)

    def test_failed_final_state_or_determinism_exits_nonzero(self):
        for case in ("inadmissible_final", "unstable_trials"):
            with self.subTest(case=case):
                self.run_case(case, 1)


if __name__ == "__main__":
    os.chdir(ROOT)
    if len(sys.argv) == 3 and sys.argv[1] == "--ev-gate-case":
        ev_gate_subprocess(sys.argv[2])
    else:
        unittest.main(verbosity=2)
