"""
Atomic persistence of (successor graph, trace) for one evaluation step
(reviewer concern R1-8).

Protocol (write-ahead, pointer-switch):

  step_id = SHA-256("step" || digest(G_t) || window_id)   -- deterministic

  1. Stage:   write steps/<step_id>/trace.json   (temp file + os.replace)
              write steps/<step_id>/state.nt     (temp file + os.replace)
              both files carry the successor digest and the parent step
              identifier; fsync each.
  2. Commit:  atomically replace the CURRENT pointer file with
              {"step_id", "successor_digest", "parent_step_id"} (temp + os.replace).
     os.replace is atomic on POSIX and on Windows (NTFS), so CURRENT either
     still names the previous step or fully names the new one; it never
     names a half-written step.

The committed history is the parent-linked chain from CURRENT back to the
first step; every committed step records its parent, so the chain is derived
from the committed artifacts themselves and needs no separate log.

Recovery (on startup):
  * read CURRENT; verify that steps/<step_id>/state.nt exists, that its
    recomputed digest equals the recorded successor digest, and that
    trace.json exists and records the same digest (top level and nested);
  * walk the parent chain and verify every committed predecessor;
  * any steps/<id>/ directory that is NOT on the committed chain is an
    incomplete staging of a crashed step: it is discarded; committed history
    is never deleted;
  * a commit whose input digest is not the successor digest of CURRENT is a
    stale or out-of-order step and is refused (parent check); re-executing
    the same (G_t, W_t) yields the same step_id, which is refused as a
    duplicate of a committed step;
  * a submitted (graph, trace) pair whose digests disagree is refused before
    anything is staged, and no commit proceeds while the committed chain does
    not verify (pointer/trace parent agreement, no cycles, no missing parents,
    input/successor linkage); recovery never deletes from an unverified store;
  * the processed-event ledger after the step is part of the committed trace
    (run_controller.py), so a restart resumes from CURRENT's state and ledger.
Single writer is assumed; no concurrency protocol is implemented.

Consistency invariant: at every point in time, the graph named by CURRENT
and the trace named by CURRENT verify against each other; the authoritative
state and the audit record never diverge.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

from rdflib import Graph


class CrashPoint(Exception):
    """Raised by tests to simulate a crash at a given point."""


def graph_canonical_nt(graph: Graph) -> str:
    return "\n".join(sorted(line for line in graph.serialize(format="nt").split("\n") if line))


def graph_digest(graph: Graph) -> str:
    return hashlib.sha256(graph_canonical_nt(graph).encode("utf-8")).hexdigest()


def step_id_for(input_digest: str, window_id: str) -> str:
    return hashlib.sha256(f"step|{input_digest}|{window_id}".encode("utf-8")).hexdigest()[:32]


def _atomic_write(path: Path, data: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


class StepStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.steps_dir = self.root / "steps"
        self.current_path = self.root / "CURRENT"
        self.steps_dir.mkdir(parents=True, exist_ok=True)

    # -- commit ---------------------------------------------------------------

    def commit_step(
        self,
        input_digest: str,
        window_id: str,
        successor_graph: Graph,
        trace: Dict[str, Any],
        crash_after: Optional[str] = None,   # "trace" | "state" | None
        ledger_after: Optional[list] = None, # processed-event ledger after this step (consume-once)
    ) -> str:
        step_id = step_id_for(input_digest, window_id)

        # Validate the submitted pair BEFORE anything is staged: the trace must
        # describe exactly this successor graph, and the chain must be intact.
        successor_digest = graph_digest(successor_graph)
        nested = (trace.get("successor_graph") or {}).get("digest")
        if nested is not None and nested != successor_digest:
            raise ValueError("trace/state mismatch: the trace's nested successor digest is not the digest of the submitted graph")
        recorded_lines = (trace.get("successor_graph") or {}).get("triples")
        if recorded_lines is not None and \
                hashlib.sha256("\n".join(sorted(recorded_lines)).encode("utf-8")).hexdigest() != successor_digest:
            raise ValueError("trace/state mismatch: the trace's successor snapshot does not serialize to the submitted graph")
        recorded_input = (trace.get("input_graph") or {}).get("digest")
        if recorded_input is not None and recorded_input != input_digest:
            raise ValueError("trace/state mismatch: the trace's input digest is not the submitted input digest")
        chain_state = self.verify_current()
        if not chain_state["consistent"]:
            raise ValueError(f"store is inconsistent, refusing to commit: {chain_state['reason']}")

        current = self.read_current()
        if step_id in self.committed_chain():
            raise ValueError(f"duplicate step {step_id}: already committed (idempotence guard)")
        if current is not None and current.get("successor_digest") != input_digest:
            raise ValueError(
                f"stale step {step_id}: input digest {input_digest[:12]} is not the successor "
                f"digest of the committed step {current['step_id'][:12]} (parent check)")
        parent_step_id = current["step_id"] if current else None

        step_dir = self.steps_dir / step_id
        step_dir.mkdir(parents=True, exist_ok=True)

        trace = dict(trace)
        trace["successor_digest"] = successor_digest
        trace["step_id"] = step_id
        trace["parent_step_id"] = parent_step_id
        trace["input_digest"] = input_digest
        if ledger_after is not None:
            trace["processed_event_ledger_after_step"] = sorted(set(map(str, ledger_after)))

        _atomic_write(step_dir / "trace.json", json.dumps(trace, indent=2, default=str))
        if crash_after == "trace":
            raise CrashPoint("crash after trace, before state")

        _atomic_write(step_dir / "state.nt", graph_canonical_nt(successor_graph))
        if crash_after == "state":
            raise CrashPoint("crash after state, before pointer switch")

        _atomic_write(self.current_path,
                      json.dumps({"step_id": step_id, "successor_digest": successor_digest,
                                  "parent_step_id": parent_step_id}))
        return step_id

    # -- committed history -----------------------------------------------------

    def _read_trace(self, step_id: str) -> Optional[Dict[str, Any]]:
        trace_path = self.steps_dir / step_id / "trace.json"
        if not trace_path.exists():
            return None
        return json.loads(trace_path.read_text(encoding="utf-8"))

    def committed_chain(self) -> list:
        """Step identifiers on the parent chain from CURRENT to the first step.
        Use chain_status() to learn whether the chain is intact."""
        return self.chain_status()["chain"]

    def chain_status(self) -> Dict[str, Any]:
        """Walk the parent chain with a cycle guard and report its integrity:
        the pointer's parent must equal the current trace's parent, every
        parent named by a trace must exist, no step may reach itself, and each
        step's input digest must be its parent's successor digest."""
        current = self.read_current()
        if current is None:
            return {"chain": [], "intact": True, "reason": "empty store"}
        chain, seen = [], set()
        step_id = current["step_id"]
        prev_input = None
        while step_id:
            if step_id in seen:
                return {"chain": chain, "intact": False, "reason": f"cycle at {step_id[:12]}"}
            seen.add(step_id)
            trace = self._read_trace(step_id)
            if trace is None:
                return {"chain": chain, "intact": False, "reason": f"missing trace for {step_id[:12]}"}
            chain.append(step_id)
            if len(chain) == 1 and trace.get("parent_step_id") != current.get("parent_step_id"):
                return {"chain": chain, "intact": False, "reason": "pointer and trace disagree on the parent step"}
            if prev_input is not None and trace.get("successor_digest") != prev_input:
                return {"chain": chain, "intact": False,
                        "reason": f"successor digest of {step_id[:12]} is not the input digest of its child"}
            prev_input = trace.get("input_digest")
            step_id = trace.get("parent_step_id")
        return {"chain": chain, "intact": True, "reason": "intact"}

    def current_ledger(self) -> list:
        """Processed-event ledger after the currently committed step."""
        current = self.read_current()
        if current is None:
            return []
        trace = self._read_trace(current["step_id"]) or {}
        return list(trace.get("processed_event_ledger_after_step", []))

    def current_state(self) -> Optional[Graph]:
        current = self.read_current()
        if current is None:
            return None
        graph = Graph()
        graph.parse(data=(self.steps_dir / current["step_id"] / "state.nt").read_text(encoding="utf-8"), format="nt")
        return graph

    # -- read / verify --------------------------------------------------------

    def read_current(self) -> Optional[Dict[str, Any]]:
        if not self.current_path.exists():
            return None
        return json.loads(self.current_path.read_text(encoding="utf-8"))

    def verify_step(self, step_id: str, expected_digest: Optional[str] = None) -> Dict[str, Any]:
        step_dir = self.steps_dir / step_id
        state_path = step_dir / "state.nt"
        trace_path = step_dir / "trace.json"
        if not state_path.exists() or not trace_path.exists():
            return {"consistent": False, "reason": "missing artefacts", "step_id": step_id}
        graph = Graph()
        graph.parse(data=state_path.read_text(encoding="utf-8"), format="nt")
        recomputed = graph_digest(graph)
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
        nested = (trace.get("successor_graph") or {}).get("digest", recomputed)
        lines = (trace.get("successor_graph") or {}).get("triples")
        snapshot_ok = lines is None or \
            hashlib.sha256("\n".join(sorted(lines)).encode("utf-8")).hexdigest() == recomputed
        ok = recomputed == trace.get("successor_digest") == nested and snapshot_ok
        if expected_digest is not None:
            ok = ok and recomputed == expected_digest
        return {"consistent": ok, "reason": "verified" if ok else "digest mismatch", "step_id": step_id}

    def verify_current(self) -> Dict[str, Any]:
        """Verify CURRENT and every committed predecessor on the parent chain."""
        current = self.read_current()
        if current is None:
            return {"consistent": True, "reason": "empty store"}
        status = self.chain_status()
        if not status["intact"]:
            return {"consistent": False, "reason": f"broken chain: {status['reason']}", "step_id": current["step_id"]}
        result = self.verify_step(current["step_id"], current["successor_digest"])
        if not result["consistent"]:
            return result
        for step_id in status["chain"][1:]:
            predecessor = self.verify_step(step_id)
            if not predecessor["consistent"]:
                return {"consistent": False, "reason": f"committed predecessor {step_id[:12]} fails verification",
                        "step_id": current["step_id"]}
        result["chain_length"] = len(status["chain"])
        return result

    # -- recovery -------------------------------------------------------------

    def recover(self) -> Dict[str, Any]:
        current = self.read_current()
        committed = current["step_id"] if current else None
        verification = self.verify_current()
        if not verification["consistent"]:
            # Never delete anything from a store whose committed chain does not
            # verify: report and leave every directory in place for repair.
            return {"committed_step": committed, "committed_chain": self.chain_status()["chain"],
                    "discarded_incomplete_steps": [], "verification": verification, "recovery_refused": True}
        chain = set(self.chain_status()["chain"])
        discarded = []
        for step_dir in sorted(self.steps_dir.iterdir()):
            if step_dir.is_dir() and step_dir.name not in chain:
                shutil.rmtree(step_dir)
                discarded.append(step_dir.name)
        return {"committed_step": committed,
                "committed_chain": sorted(chain),
                "discarded_incomplete_steps": discarded,
                "verification": self.verify_current()}
