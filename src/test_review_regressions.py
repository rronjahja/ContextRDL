"""Regression checks for the targeted peer-review corrections.

Run from the repository root: python src/test_review_regressions.py
These checks do not certify the open issues documented in the review report.
"""
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Runs from the repository root or from src/.
HERE = Path(__file__).resolve().parent
if (HERE / "rule_engine.py").exists():
    sys.path.insert(0, str(HERE))
    os.chdir(HERE.parent)

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, XSD

from admissibility import check_admissibility_incremental, check_admissibility_shacl, supports_incremental_shapes
from dataset_builder import build_dataset, load_state, select_window_events
from engine import run_engine
from replay_full import replay_full
from resolver import resolve_actions
from resolver_incremental import resolve_actions_incremental
from rule_engine import evaluate_rules, load_settings
from rule_loader import load_rules, validate_rules
from trace import graph_from_snapshot, save_trace, serialize_graph_snapshot

EX = Namespace("http://example.org/building#")


class ReviewRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state, cls.schedule, cls.accepted, cls.successor, cls.trace = run_engine(save_trace_file=False)

    def test_original_default_result_is_preserved(self):
        self.assertEqual([a["rid"] for a in self.accepted], ["r6", "r5", "r2", "r7", "r8"])
        self.assertEqual(self.trace["successor_graph"]["digest"],
                         "4bbb205b597fc0b5310d6f4b1338347fc014cccf8de131cbd63e87bfb563014f")

    def test_duplicate_rule_identifiers_rejected(self):
        rules = load_rules("configs/rules.json")
        with self.assertRaises(ValueError):
            validate_rules(rules + [copy.deepcopy(rules[0])])
        with self.assertRaises(ValueError):
            run_engine(rules=rules + [copy.deepcopy(rules[0])], save_trace_file=False)

    def test_multiline_literal_snapshot_roundtrip(self):
        graph = Graph()
        graph.add((URIRef("urn:s"), URIRef("urn:p"), Literal('line1\nline2 "quote" \\')))
        self.assertEqual(set(graph), set(graph_from_snapshot(serialize_graph_snapshot(graph))))

    def test_rule_set_order_does_not_change_trace(self):
        # The semantic trace is everything except run provenance ("environment",
        # which records where the rules came from, library versions, revision).
        *_, other = run_engine(rules=list(reversed(load_rules("configs/rules.json"))), save_trace_file=False)
        semantic = lambda t: {k: v for k, v in t.items() if k != "environment"}
        self.assertEqual(semantic(self.trace), semantic(other))

    def test_inactive_actions_receive_a_decision(self):
        settings = load_settings()
        context = {"role_rank": {"emergency": 0, "operator": 1, "occupant": 2},
                   "active_roles": [], "enforce_active_roles": True}
        ds, meta = build_dataset(self.state, self.trace["events"], settings=settings)
        enabled = evaluate_rules(ds, load_rules("configs/rules.json"), settings, context, meta)
        self.assertEqual(len(enabled), 8)
        settings["governance"].update(active_roles=[], enforce_active_roles=True)
        accepted, _, decisions = resolve_actions(self.state, self.schedule, settings=settings)
        self.assertEqual(accepted, [])
        self.assertTrue(all(d["reason"] == "inactive_role" for d in decisions))

    def test_enumerations_preserve_rdf_term_identity(self):
        for value in (Literal("normal", lang="en"), Literal("normal", datatype=URIRef("urn:custom"))):
            graph = copy.deepcopy(self.state)
            graph.set((EX.ZoneB, EX.ventilationMode, value))
            self.assertFalse(check_admissibility_incremental(graph)[0])
            self.assertFalse(check_admissibility_shacl(graph)[0])

    def test_nonfinite_setpoint_not_accepted_by_fast_validator(self):
        graph = copy.deepcopy(self.state)
        graph.set((EX.ZoneB, EX.currentSetpoint, Literal(float("nan"), datatype=XSD.double)))
        self.assertFalse(check_admissibility_incremental(graph)[0])

    def test_changed_local_shapes_use_reference_validator(self):
        shapes = Path("shapes/invariants.ttl").read_text()
        shapes += '\nex:ReviewCap a sh:NodeShape ; sh:targetNode ex:ZoneB ; sh:property [ sh:path ex:currentSetpoint ; sh:maxInclusive 21.0 ] .\n'
        action = next(a for a in self.schedule if a["rid"] == "r7")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shapes.ttl"; path.write_text(shapes)
            for resolver in (resolve_actions, resolve_actions_incremental):
                acc, graph, decisions = resolver(self.state, [action], shapes_path=str(path))
                self.assertFalse(acc)
                self.assertTrue(check_admissibility_shacl(graph, str(path))[0])
                self.assertEqual(decisions[0]["reason"], "inadmissible")

    def test_windows_newlines_preserve_compiled_shape_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shapes.ttl"
            text = Path("shapes/invariants.ttl").read_text()
            path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
            self.assertTrue(supports_incremental_shapes(str(path)))

    def test_empty_windows_keep_anchor_identity(self):
        ids = [select_window_events([], load_settings(), anchor)[1]["window_id"]
               for anchor in ("2026-03-14T10:00:00Z", "2026-03-14T10:05:00Z")]
        self.assertNotEqual(*ids)

    def test_noop_has_empty_graph_delta(self):
        graph = copy.deepcopy(self.state)
        graph.set((EX.ZoneB, EX.currentSetpoint, Literal(20.0, datatype=XSD.decimal)))
        action = copy.deepcopy(next(a for a in self.schedule if a["rid"] == "r7"))
        action["value"] = 20.0
        old = resolve_actions(graph, [action])[2]
        new = resolve_actions_incremental(graph, [action], record_digests=True)[2]
        self.assertEqual(old, new)
        self.assertEqual(new[0]["removed_triples"], [])
        self.assertEqual(new[0]["inserted_triples"], [])

    def test_replay_rejects_corrupted_decision_delta(self):
        bad = copy.deepcopy(self.trace)
        bad["decisions"][0]["inserted_triples"] = ["CORRUPTED"]
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "trace.json")
            save_trace(bad, path)
            self.assertFalse(replay_full(path)["overall_pass"])




class V3Regressions(unittest.TestCase):
    """Regression checks for the defects found in the second audit."""

    @classmethod
    def setUpClass(cls):
        cls.state, cls.schedule, cls.accepted, cls.successor, cls.trace = run_engine(save_trace_file=False)

    def test_decimal_boundaries_are_exact(self):
        for lexical in ("26.0000000000000000000001", "17.9999999999999999999999"):
            graph = copy.deepcopy(self.state)
            graph.set((EX.ZoneB, EX.currentSetpoint, Literal(lexical, datatype=XSD.decimal)))
            self.assertFalse(check_admissibility_incremental(graph)[0])
            self.assertFalse(check_admissibility_shacl(graph)[0])

    def test_zone_a_node_shapes_apply_without_class_assertion(self):
        graph = copy.deepcopy(self.state)
        graph.remove((EX.ZoneA, None, None))
        graph.add((EX.ZoneA, EX.currentSetpoint, Literal(24.0, datatype=XSD.decimal)))
        self.assertEqual(check_admissibility_incremental(graph)[0], check_admissibility_shacl(graph)[0])
        self.assertFalse(check_admissibility_incremental(graph)[0])

    def test_changed_shape_file_at_same_path_is_reparsed(self):
        from resolver import resolve_actions as ref
        action = next(a for a in self.schedule if a["rid"] == "r7")  # ZoneB := 22
        base = Path("shapes/invariants.ttl").read_text()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shapes.ttl"
            path.write_text(base + '\nex:Cap a sh:NodeShape ; sh:targetNode ex:ZoneB ; sh:property [ sh:path ex:currentSetpoint ; sh:maxInclusive 24.0 ] .\n')
            self.assertTrue(ref(self.state, [action], shapes_path=str(path))[0])
            path.write_text(base + '\nex:Cap a sh:NodeShape ; sh:targetNode ex:ZoneB ; sh:property [ sh:path ex:currentSetpoint ; sh:maxInclusive 21.0 ] .\n')
            acc, graph, _ = ref(self.state, [action], shapes_path=str(path))
            self.assertFalse(acc)
            self.assertTrue(check_admissibility_shacl(graph, str(path))[0])

    def test_inline_rules_replay_from_embedded_rule_set(self):
        rules = copy.deepcopy(load_rules("configs/rules.json"))
        for r in rules:
            if r["rid"] == "r4":
                r["priority"] = 0  # r4 now precedes r7 within the occupant/operator order? no: roles differ,
                # so change the operator preheat instead: make r7 lose to r4 by moving r7 to the occupant role
                pass
        for r in rules:
            if r["rid"] == "r7":
                r["issuing_role"] = "occupant"; r["priority"] = 5
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "trace.json")
            *_, trace = run_engine(rules=rules, trace_path=path)
            self.assertEqual(trace["environment"]["rules_source"], "inline")
            self.assertNotEqual(trace["successor_graph"]["digest"], self.trace["successor_graph"]["digest"])
            self.assertTrue(replay_full(path)["overall_pass"])

    def test_replay_restores_environment_and_validates_profile(self):
        import os
        os.environ.pop("ADMISSIBILITY_REGIME", None)
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "trace.json")
            t = copy.deepcopy(self.trace); t["settings"]["admissibility_regime"] = "shacl"; save_trace(t, path)
            self.assertTrue(replay_full(path)["overall_pass"])
            self.assertIsNone(os.environ.get("ADMISSIBILITY_REGIME"))
            t = copy.deepcopy(self.trace); t["settings"]["dependencies"]["binding_profile"] = "unknown-9"; save_trace(t, path)
            self.assertFalse(replay_full(path)["overall_pass"])

    def test_replay_rejects_corrupted_snapshots_and_records(self):
        corruptions = {
            "input_digest": lambda t: t["input_graph"].__setitem__("digest", "0" * 64),
            "successor_triples": lambda t: t["successor_graph"].__setitem__("triples", []),
            "enabled_value": lambda t: t["enabled_actions"][0].__setitem__("value", "corrupted"),
            "schedule_value": lambda t: t["schedule"][0].__setitem__("value", "corrupted"),
            "rules_snapshot": lambda t: t["rules"][0].__setitem__("priority", 99),
        }
        for name, corrupt in corruptions.items():
            bad = copy.deepcopy(self.trace); corrupt(bad)
            with tempfile.TemporaryDirectory() as tmp:
                path = str(Path(tmp) / "trace.json"); save_trace(bad, path)
                self.assertFalse(replay_full(path)["overall_pass"], name)

    def test_persistence_refuses_broken_chains_and_inconsistent_pairs(self):
        from persistence import StepStore, graph_digest
        from experiment_helpers import tie_conflict_events, governance_conflict_events
        with tempfile.TemporaryDirectory() as tmp:
            store, graph, ids = StepStore(Path(tmp)), None, []
            for events in (None, tie_conflict_events(), governance_conflict_events()):
                g_in, _, _, succ, tr = run_engine(save_trace_file=False, state_graph=graph, events=events)
                ids.append(store.commit_step(graph_digest(g_in), tr["window"]["window_id"], succ, dict(tr))); graph = succ
            # inconsistent pair refused before staging
            bad = dict(tr); bad["successor_graph"] = dict(bad["successor_graph"], digest="0" * 64)
            with self.assertRaises(ValueError):
                store.commit_step(graph_digest(succ), "w-x", succ, bad)
            # truncated parent link: recovery refuses to delete anything
            tp = store.steps_dir / ids[2] / "trace.json"; t = json.loads(tp.read_text()); t["parent_step_id"] = None; tp.write_text(json.dumps(t))
            rec = store.recover()
            self.assertTrue(rec.get("recovery_refused")); self.assertEqual(rec["discarded_incomplete_steps"], [])
            self.assertTrue(all((store.steps_dir / i).exists() for i in ids))
            # cycle: terminates and is reported
            t["parent_step_id"] = ids[2]; tp.write_text(json.dumps(t))
            self.assertFalse(store.verify_current()["consistent"])

    def test_ledger_uses_exact_identifiers(self):
        from persistence import StepStore
        from run_controller import run_committed_step
        log = [{"eid": "future-suffix", "timestamp": "2026-03-14T09:00:00Z", "type": "OccupancyDetected", "role": "occupant",
                "payload": {"zone": str(EX.ZoneB)}},
               {"eid": "future", "timestamp": "2026-03-14T09:07:30Z", "type": "PreheatRequest", "role": "operator",
                "payload": {"zone": str(EX.ZoneB), "target": 23}}]
        with tempfile.TemporaryDirectory() as tmp:
            store = StepStore(Path(tmp))
            first = run_committed_step(store, log, "2026-03-14T09:04:00Z", initial_graph=self.state)
            self.assertEqual(first["ledger_after"], ["future-suffix"])
            third = run_committed_step(store, log, "2026-03-14T09:08:00Z")
            self.assertEqual(third["accepted_rids"], ["r7"])




class V4Regressions(unittest.TestCase):
    """Regression checks for the defects found in the third audit."""

    @classmethod
    def setUpClass(cls):
        cls.state, cls.schedule, cls.accepted, cls.successor, cls.trace = run_engine(save_trace_file=False)

    def _vent_action(self, zone, value):
        action = copy.deepcopy(next(a for a in self.schedule if a["rid"] == "r8"))  # ventilationMode single-slot write
        action.update({"zone": str(zone), "value": value, "target_key": f"{zone}|{EX.ventilationMode}"})
        return action

    def test_untyped_zone_a_with_two_setpoints_and_off(self):
        from admissibility import fast_path_eligible
        from resolver import resolve_actions as ref
        graph = copy.deepcopy(self.state)
        graph.remove((EX.ZoneA, RDF.type, EX.HVAC_Zone))
        graph.add((EX.ZoneA, EX.currentSetpoint, Literal(20.0, datatype=XSD.decimal)))
        self.assertTrue(check_admissibility_shacl(graph)[0])
        self.assertFalse(fast_path_eligible(graph)[0])  # outside the declared fragment: pySHACL is used
        for resolver in (ref, resolve_actions_incremental):
            acc, succ, decisions = resolver(graph, [self._vent_action(EX.ZoneA, "off")])
            self.assertEqual(acc, [], resolver.__name__)
            self.assertEqual(check_admissibility_shacl(succ)[0], True)

    def test_subclass_typed_zone_is_targeted(self):
        from rdflib.namespace import RDFS
        from admissibility import fast_path_eligible
        from resolver import resolve_actions as ref
        graph = copy.deepcopy(self.state)
        graph.remove((EX.ZoneB, RDF.type, EX.HVAC_Zone))
        graph.add((EX.ZoneB, RDF.type, EX.SpecialZone))
        graph.add((EX.SpecialZone, RDFS.subClassOf, EX.HVAC_Zone))
        self.assertTrue(check_admissibility_shacl(graph)[0])
        self.assertFalse(fast_path_eligible(graph)[0])
        # the hand-written checker also honours the subclass closure on its own
        bad = copy.deepcopy(graph); bad.set((EX.ZoneB, EX.ventilationMode, Literal("turbo")))
        self.assertFalse(check_admissibility_incremental(bad)[0])
        for resolver in (ref, resolve_actions_incremental):
            acc, succ, _ = resolver(graph, [self._vent_action(EX.ZoneB, "turbo")])
            self.assertEqual(acc, [], resolver.__name__)

    def test_replay_detects_window_identity_corruption(self):
        bad = copy.deepcopy(self.trace); bad["window"]["window_id"] = "CORRUPTED_WINDOW_ID"
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "trace.json"); save_trace(bad, path)
            self.assertFalse(replay_full(path)["overall_pass"])

    def test_replay_without_recorded_context_and_explicit_contexts_path(self):
        t = copy.deepcopy(self.trace); t["window"].pop("governance_context", None)
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "trace.json"); save_trace(t, path)
            self.assertTrue(replay_full(path, contexts_path="data/contexts.json")["overall_pass"])

    def test_cross_implementation_handles_empty_pipeline_trace(self):
        from experiment_cross_implementation import compare
        shapes = Graph(); shapes.parse("shapes/invariants.ttl", format="turtle")
        *_, empty = run_engine(events=[], anchor_timestamp="2026-03-14T12:00:00Z", save_trace_file=False)
        report = compare(empty, shapes)
        self.assertTrue(report["digest_match"] and report["schedule_match"])

    def test_store_rejects_trace_with_hollow_successor_snapshot(self):
        from persistence import StepStore, graph_digest
        with tempfile.TemporaryDirectory() as tmp:
            store = StepStore(Path(tmp))
            bad = copy.deepcopy(self.trace); bad["successor_graph"]["triples"] = []
            with self.assertRaises(ValueError):
                store.commit_step(graph_digest(self.state), self.trace["window"]["window_id"], self.successor, bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
