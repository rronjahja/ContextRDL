"""
Atomic persistence of (successor graph, trace) for one evaluation step
(reviewer concern R1-8).

Protocol (write-ahead, pointer-switch):

  step_id = SHA-256("step" || digest(G_t) || window_id)   -- deterministic

  1. Stage:   write steps/<step_id>/trace.json   (temp file + os.replace)
              write steps/<step_id>/state.nt     (temp file + os.replace)
              both files carry the successor digest; fsync each.
  2. Commit:  atomically replace the CURRENT pointer file with
              {"step_id": ..., "successor_digest": ...} (temp + os.replace).
     os.replace is atomic on POSIX and on Windows (NTFS), so CURRENT either
     still names the previous step or fully names the new one; it never
     names a half-written step.

Recovery (on startup):
  * read CURRENT; verify that steps/<step_id>/state.nt exists and that its
    recomputed digest equals the recorded successor digest, and that
    trace.json exists and internally records the same digest;
  * any steps/<id>/ directory NOT named by CURRENT is an incomplete staging
    of a crashed step: it is discarded (the step never committed);
  * duplicate replay after recovery is prevented by the deterministic
    step_id: re-executing the same (G_t, W_t) yields the same step_id, and
    the runtime refuses to commit a step_id equal to CURRENT's.

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
    return "\n".join(sorted(f"{s.n3()} {p.n3()} {o.n3()} ." for (s, p, o) in graph))


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
    ) -> str:
        step_id = step_id_for(input_digest, window_id)

        current = self.read_current()
        if current and current.get("step_id") == step_id:
            raise ValueError(f"duplicate step {step_id}: already committed (idempotence guard)")

        step_dir = self.steps_dir / step_id
        step_dir.mkdir(parents=True, exist_ok=True)

        successor_digest = graph_digest(successor_graph)
        trace = dict(trace)
        trace["successor_digest"] = successor_digest
        trace["step_id"] = step_id

        _atomic_write(step_dir / "trace.json", json.dumps(trace, indent=2, default=str))
        if crash_after == "trace":
            raise CrashPoint("crash after trace, before state")

        _atomic_write(step_dir / "state.nt", graph_canonical_nt(successor_graph))
        if crash_after == "state":
            raise CrashPoint("crash after state, before pointer switch")

        _atomic_write(self.current_path,
                      json.dumps({"step_id": step_id, "successor_digest": successor_digest}))
        return step_id

    # -- read / verify --------------------------------------------------------

    def read_current(self) -> Optional[Dict[str, Any]]:
        if not self.current_path.exists():
            return None
        return json.loads(self.current_path.read_text(encoding="utf-8"))

    def verify_current(self) -> Dict[str, Any]:
        current = self.read_current()
        if current is None:
            return {"consistent": True, "reason": "empty store"}
        step_dir = self.steps_dir / current["step_id"]
        state_path = step_dir / "state.nt"
        trace_path = step_dir / "trace.json"
        if not state_path.exists() or not trace_path.exists():
            return {"consistent": False, "reason": "CURRENT names missing artefacts"}
        graph = Graph()
        graph.parse(data=state_path.read_text(encoding="utf-8"), format="nt")
        recomputed = graph_digest(graph)
        recorded = current["successor_digest"]
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
        ok = recomputed == recorded == trace.get("successor_digest")
        return {"consistent": ok,
                "reason": "verified" if ok else "digest mismatch",
                "step_id": current["step_id"]}

    # -- recovery -------------------------------------------------------------

    def recover(self) -> Dict[str, Any]:
        current = self.read_current()
        committed = current["step_id"] if current else None
        discarded = []
        for step_dir in self.steps_dir.iterdir():
            if step_dir.is_dir() and step_dir.name != committed:
                shutil.rmtree(step_dir)
                discarded.append(step_dir.name)
        verification = self.verify_current()
        return {"committed_step": committed,
                "discarded_incomplete_steps": discarded,
                "verification": verification}
