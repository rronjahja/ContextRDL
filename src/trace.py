from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from typing import Any, Dict, Iterable, List, Mapping, Tuple
from pathlib import Path
from rdflib import Graph


def canonical_triple_lines(graph: Graph) -> List[str]:
    # Literal.n3() may emit Turtle long strings, which are not N-Triples.
    return sorted(line for line in graph.serialize(format="nt").split("\n") if line)


def file_sha256(path: str) -> str:
    p = Path(path)
    if not p.is_absolute() and not p.exists():
        p = Path(__file__).resolve().parent.parent / p
    return hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def graph_digest(graph: Graph) -> str:
    payload = "\n".join(canonical_triple_lines(graph))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def graph_delta(before: Graph, after: Graph) -> Tuple[List[str], List[str]]:
    before_lines = set(canonical_triple_lines(before))
    after_lines = set(canonical_triple_lines(after))
    removed = sorted(before_lines - after_lines)
    inserted = sorted(after_lines - before_lines)
    return removed, inserted


def serialize_graph_snapshot(graph: Graph) -> Dict[str, Any]:
    lines = canonical_triple_lines(graph)
    return {
        "triples": lines,
        "digest": hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest(),
        "triple_count": len(lines),
    }


def graph_from_snapshot(snapshot: Mapping[str, Any]) -> Graph:
    graph = Graph()
    triples = snapshot.get("triples", [])
    if triples:
        graph.parse(data="\n".join(triples), format="nt")
    return graph


def _environment() -> Dict[str, Any]:
    import platform
    import sys
    info: Dict[str, Any] = {"python": sys.version.split()[0], "platform": platform.platform()}
    try:
        import rdflib
        info["rdflib"] = rdflib.__version__
    except Exception:
        pass
    try:
        import pyshacl
        info["pyshacl"] = pyshacl.__version__
    except Exception:
        pass
    root = Path(__file__).resolve().parent.parent
    try:
        git = root / ".git"
        git_dir = git
        if git.is_file():  # worktree: ".git" is a pointer file
            git_dir = Path(git.read_text(encoding="utf-8").split("gitdir:", 1)[1].strip())
            if not git_dir.is_absolute():
                git_dir = (root / git_dir).resolve()
        ref = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if ref.startswith("ref: "):
            ref_path = git_dir / ref[5:]
            common = git_dir / "commondir"
            if not ref_path.exists() and common.exists():
                ref_path = (git_dir / common.read_text(encoding="utf-8").strip()).resolve() / ref[5:]
            ref = ref_path.read_text(encoding="utf-8").strip()
        info["source_revision"] = ref
    except Exception:
        pass
    # Content fingerprint of the sources actually executed (catches local
    # modifications that the git revision alone cannot).
    try:
        digest = hashlib.sha256()
        for rel in ("src", "ev", "configs", "shapes", "data"):
            base = root / rel
            if not base.exists():
                continue
            for path in sorted(base.rglob("*")):
                if path.is_file() and path.suffix in {".py", ".json", ".ttl", ".jsonl"} and "results" not in path.parts \
                        and "__pycache__" not in path.parts:
                    digest.update(str(path.relative_to(root)).replace("\\", "/").encode("utf-8"))
                    digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
        info["source_manifest_sha256"] = digest.hexdigest()
    except Exception:
        pass
    return info


def _jsonable_action(action: Mapping[str, Any]) -> Dict[str, Any]:
    return json.loads(json.dumps(action, sort_keys=True, default=str))


def _rules_snapshot(rules: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    snapshot: List[Dict[str, Any]] = []
    for rule in sorted(rules, key=lambda item: item["rid"]):
        snapshot.append(
            {
                "rid": rule.get("rid"),
                "priority": rule.get("priority"),
                "issuing_role": rule.get("issuing_role"),
                "predicate": (rule.get("insert_template") or {}).get("predicate"),
                "value_expr": deepcopy((rule.get("insert_template") or {}).get("value_expr")),
            }
        )
    return snapshot


def build_trace(
    input_graph: Graph,
    enabled_actions: Iterable[Mapping[str, Any]],
    schedule: Iterable[Mapping[str, Any]],
    accepted_actions: Iterable[Mapping[str, Any]],
    successor_graph: Graph,
    decisions: Iterable[Mapping[str, Any]],
    settings: Mapping[str, Any] | None = None,
    window_meta: Mapping[str, Any] | None = None,
    rules: Iterable[Mapping[str, Any]] | None = None,
    events: Iterable[Mapping[str, Any]] | None = None,
) -> Dict[str, Any]:
    enabled_list = sorted(
        (_jsonable_action(action) for action in enabled_actions),
        key=lambda action: (action["rid"], action["bindKey"], action.get("window_id", ""), action["aid"]),
    )
    schedule_list = [_jsonable_action(action) for action in schedule]
    accepted_list = [_jsonable_action(action) for action in accepted_actions]
    decisions_list = json.loads(json.dumps(list(decisions), sort_keys=True, default=str))

    trace = {
        "trace_version": "2.2",
        "settings": deepcopy(settings) if settings is not None else {},
        # Provenance of the recording run (not part of the execution
        # configuration; not compared by replay).
        "environment": _environment(),
        "window": deepcopy(window_meta) if window_meta is not None else {},
        "rules": _rules_snapshot(rules or []),
        "events": json.loads(json.dumps(list(events or []), sort_keys=True, default=str)),
        "input_graph": serialize_graph_snapshot(input_graph),
        "enabled_actions": enabled_list,
        "schedule": schedule_list,
        "accepted_actions": accepted_list,
        "decisions": decisions_list,
        "successor_graph": serialize_graph_snapshot(successor_graph),
        "summary": {
            "enabled_count": len(enabled_list),
            "scheduled_count": len(schedule_list),
            "accepted_count": len(accepted_list),
            "rejected_count": len(schedule_list) - len(accepted_list),
        },
    }
    return trace


def save_trace(trace: Mapping[str, Any], path: str = "results/hvac/traces/trace.json") -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(trace, handle, indent=2, sort_keys=True)


def load_trace(path: str = "results/hvac/traces/trace.json") -> Dict[str, Any]:
    p = Path(path)
    if not p.is_absolute() and not p.exists():
        p = Path(__file__).resolve().parent.parent / p
    with open(p, "r", encoding="utf-8") as handle:
        return json.load(handle)


if __name__ == "__main__":
    graph = Graph()
    graph.parse("base_graph.ttl", format="turtle")
    snapshot = serialize_graph_snapshot(graph)
    rebuilt = graph_from_snapshot(snapshot)

    print("Original digest:", graph_digest(graph))
    print("Rebuilt digest:", graph_digest(rebuilt))
