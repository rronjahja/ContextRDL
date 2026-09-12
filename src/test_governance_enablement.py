"""Focused tests for the HVAC event-role conditions.

Run from the repository root with:
    python src/test_governance_enablement.py

The four recorded HVAC traces are read, never overwritten. To compare with
an older results archive, set CONTEXTRDL_ARCHIVE_TRACES to its hvac/traces
directory. Temporary traces are written only inside TemporaryDirectory.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "ev"))

from rdflib import URIRef

import experiment_ev as ev

from dataset_builder import build_dataset, load_events, prop_uri
from engine import run_engine
from replay_full import replay_full
from resolver import resolve_actions
from rule_engine import evaluate_rules, load_settings, resolve_governance_context
from rule_loader import load_rules
from trace import graph_from_snapshot, save_trace


FIXTURES = ("default", "tie_conflict", "governance_op_gt_occ", "governance_occ_gt_op")


def _semantic_decisions(trace):
    # Validator report prose can vary across platforms/library versions.
    # Every machine-readable decision field, including deltas, is compared.
    return [{k: v for k, v in decision.items() if k != "validation_report"}
            for decision in trace["decisions"]]


class GovernanceEnablementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        previous_directory = Path.cwd()
        os.chdir(ROOT)
        cls.addClassCleanup(os.chdir, previous_directory)
        environment = patch.dict(os.environ, {"ADMISSIBILITY_REGIME": "shacl"})
        environment.start()
        cls.addClassCleanup(environment.stop)
        cls.settings = load_settings("configs/settings.json")
        cls.context = resolve_governance_context(
            settings=cls.settings, contexts_path="data/contexts.json")
        cls.rules = load_rules("configs/rules.json")
        cls.events = load_events("data/events.jsonl")
        cls.ev_state = ev.load_state(ev.BASE_GRAPH_PATH)
        cls.ev_events = ev.load_events(ev.EVENTS_PATH)
        cls.ev_rules = ev.load_rules(ev.RULES_PATH)
        cls.state, cls.schedule, cls.accepted, cls.successor, cls.trace = run_engine(
            save_trace_file=False)
        cls.archive = Path(os.environ.get(
            "CONTEXTRDL_ARCHIVE_TRACES", str(ROOT / "results/hvac/traces")))

    def _enabled(self, events):
        dataset, metadata = build_dataset(self.state, events, settings=self.settings)
        actions = evaluate_rules(dataset, self.rules, settings=self.settings,
                                 context=self.context, window_meta=metadata)
        return dataset, metadata, actions

    def _recorded_trace(self, name):
        path = self.archive / ("trace_" + name + ".json")
        self.assertTrue(path.is_file(),
                        "Recorded trace required: %s. Set CONTEXTRDL_ARCHIVE_TRACES "
                        "to the original archive's hvac/traces directory." % path)
        return path, json.loads(path.read_text(encoding="utf-8"))

    def test_default_workload_keeps_all_rules_and_successor(self):
        self.assertEqual(len(self.events), 6)
        self.assertEqual(len(self.trace["enabled_actions"]), 8)
        self.assertEqual({a["rid"] for a in self.trace["enabled_actions"]},
                         {"r%d" % i for i in range(1, 9)})
        self.assertTrue(all(a["event_role"] == a["role"]
                            for a in self.trace["enabled_actions"]))
        self.assertEqual([a["rid"] for a in self.accepted],
                         ["r6", "r5", "r2", "r7", "r8"])
        self.assertEqual(self.trace["successor_graph"]["digest"],
                         "4bbb205b597fc0b5310d6f4b1338347fc014cccf8de131cbd63e87bfb563014f")

    def test_wrong_unknown_and_empty_roles_do_not_enable_event_rules(self):
        _, _, original_actions = self._enabled(self.events)
        for index, event in enumerate(self.events):
            wrong_role = "operator" if event["role"] != "operator" else "occupant"
            for role in (wrong_role, "unrecognized", ""):
                with self.subTest(event=event["eid"], role=role):
                    modified = copy.deepcopy(self.events)
                    modified[index]["role"] = role
                    _, _, actions = self._enabled(modified)
                    expected = [a for a in original_actions if a["event_id"] != event["eid"]]
                    self.assertLess(len(expected), len(original_actions))
                    # Complete equality also checks that other events retain
                    # their bindings, action identities and constructed values.
                    self.assertEqual(actions, expected)

    def test_missing_role_triple_does_not_enable_event_rules(self):
        _, _, original_actions = self._enabled(self.events)
        for event in self.events:
            with self.subTest(event=event["eid"]):
                dataset, metadata, _ = self._enabled(self.events)
                for graph in dataset.graphs():
                    graph.remove((URIRef("urn:event:" + event["eid"]), prop_uri("role"), None))
                actions = evaluate_rules(dataset, self.rules, settings=self.settings,
                                         context=self.context, window_meta=metadata)
                self.assertEqual(actions, [a for a in original_actions
                                           if a["event_id"] != event["eid"]])

    def test_inactive_roles_still_receive_runtime_decisions(self):
        dataset, metadata, _ = self._enabled(self.events)
        context = copy.deepcopy(self.context)
        context.update(active_roles=[], enforce_active_roles=True)
        enabled = evaluate_rules(dataset, self.rules, settings=self.settings,
                                 context=context, window_meta=metadata)
        self.assertEqual(len(enabled), 8)
        settings = copy.deepcopy(self.settings)
        settings.setdefault("governance", {}).update(active_roles=[], enforce_active_roles=True)
        accepted, successor, decisions = resolve_actions(self.state, self.schedule, settings=settings)
        self.assertEqual(accepted, [])
        self.assertEqual(set(successor), set(self.state))
        self.assertEqual(len(decisions), len(enabled))
        self.assertTrue(all(d["reason"] == "inactive_role" for d in decisions))

    def _domain_builders(self):
        return (("HVAC", self.events[0],
                 lambda events: build_dataset(self.state, events, settings=self.settings)),
                ("EV", self.ev_events[0],
                 lambda events: ev.build_dataset(self.ev_state, events)))

    def test_payload_cannot_override_reserved_event_metadata(self):
        for domain, original, build in self._domain_builders():
            for key in ("eid", "timestamp", "type", "role", "order"):
                with self.subTest(domain=domain, reserved_payload_key=key):
                    event = copy.deepcopy(original)
                    event["payload"][key] = event.get(key, 0)
                    with self.assertRaises(ValueError):
                        build([event])

    def test_conflicting_duplicate_event_records_are_rejected(self):
        for domain, original, build in self._domain_builders():
            for field in ("role", "type", "timestamp", "payload"):
                with self.subTest(domain=domain, conflicting_field=field):
                    conflicting = copy.deepcopy(original)
                    if field == "payload":
                        key = next(iter(conflicting["payload"]))
                        conflicting["payload"][key] = "different-value"
                    elif field == "timestamp":
                        # Both records remain inside the selected HVAC window.
                        conflicting[field] = "2026-03-14T09:00:01Z"
                    else:
                        conflicting[field] = "different-value"
                    with self.assertRaises(ValueError):
                        build([original, conflicting])

    def test_identical_duplicate_events_do_not_duplicate_actions(self):
        _, _, original_actions = self._enabled(self.events)
        _, _, duplicate_actions = self._enabled(self.events + [copy.deepcopy(self.events[0])])
        self.assertEqual(duplicate_actions, original_actions)
        dataset, metadata = ev.build_dataset(self.ev_state, self.ev_events)
        original_ev = ev.evaluate_rules(dataset, self.ev_rules, ev.ROLE_RANK, metadata["window_id"])
        dataset, metadata = ev.build_dataset(
            self.ev_state, self.ev_events + [copy.deepcopy(self.ev_events[0])])
        duplicate_ev = ev.evaluate_rules(dataset, self.ev_rules, ev.ROLE_RANK, metadata["window_id"])
        self.assertEqual(duplicate_ev, original_ev)

    def test_four_recorded_fixtures_keep_their_semantic_outputs(self):
        for name in FIXTURES:
            with self.subTest(fixture=name):
                _, archived = self._recorded_trace(name)
                *_, current = run_engine(
                    state_graph=graph_from_snapshot(archived["input_graph"]),
                    events=archived["events"], rules=self.rules,
                    settings_override=archived["settings"],
                    context_name=archived["window"]["governance_context"]["name"],
                    anchor_timestamp=archived["window"]["anchor_timestamp"],
                    save_trace_file=False)
                for field in ("input_graph", "enabled_actions", "schedule",
                              "accepted_actions", "successor_graph", "summary"):
                    self.assertEqual(current[field], archived[field], field)
                self.assertEqual(_semantic_decisions(current), _semantic_decisions(archived))
                # Current rules are actually used; a successful replay using
                # only the archived inline rules would not check the change.
                self.assertEqual(current["settings"]["dependencies"]["rules_inline"],
                                 sorted(self.rules, key=lambda r: r["rid"]))

    def test_new_trace_replays_with_current_inline_rules(self):
        with tempfile.TemporaryDirectory(prefix="contextrdl-role-test-") as directory:
            path = str(Path(directory) / "trace.json")
            save_trace(self.trace, path)
            report = replay_full(path)
            self.assertTrue(report["overall_pass"], report)
            self.assertTrue(replay_full(path, rules_path="configs/rules.json")["overall_pass"])

    def test_ev_operational_cases_still_satisfy_their_postconditions(self):
        # Both domains use the input-record guard, so preserve the existing
        # emergency, fault, connector and policy behavior as well.
        from experiment_operational import case_specs, run_case
        for spec in case_specs():
            with self.subTest(case=spec["name"]):
                result = run_case(spec, record=False)
                self.assertTrue(all(result["checks"].values()), result["checks"])

    def test_archived_traces_replay_with_their_own_inline_rules(self):
        for name in FIXTURES:
            with self.subTest(fixture=name):
                path, archived = self._recorded_trace(name)
                self.assertIsInstance(archived["settings"]["dependencies"]["rules_inline"], list)
                # No external rules_path: old evidence remains replayable even
                # though the shipped rule-query text has since changed.
                report = replay_full(str(path))
                self.assertTrue(report["overall_pass"], report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
