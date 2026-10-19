#!/usr/bin/env python3
"""Read the session frame ledger that the coordinator serialised for Python.

`host.frames(...)` is a `repl`-tool call; the `python` tool has no `host.frames`.
The coordinator calls it in the `repl`, writes the unmodified result to
`provenance/frames_ledger.json`, and saves it as an artifact (see the
orchestrator's "Frame ledger bridge"). Python consumers read that file here.
Standard library only.
"""
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

LEDGER = "provenance/frames_ledger.json"


def load_frames(path=LEDGER):
    """Return the `frames` list from the serialised `host.frames` result."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"{path} is missing: the coordinator must serialise the repl `host.frames` "
            "result to it before this phase runs")
    data = json.loads(p.read_text(encoding="utf-8"))
    frames = data.get("frames") if isinstance(data, dict) else None
    if not isinstance(frames, list):
        raise ValueError(f"{path} must be the host.frames result: an object with a `frames` list")
    return frames


def _parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def summarize(frames, end_at=None):
    """Agent/status counts, first timestamp and elapsed hours for the ledger."""
    if not frames:
        raise ValueError("frame ledger is empty")
    first_at = min(f["created_at"] for f in frames)
    end_at = end_at or max(f["created_at"] for f in frames)
    return {
        "total": len(frames),
        "agent_counts": Counter(f["agent_name"] for f in frames),
        "status_counts": Counter(f["status"] for f in frames),
        "first_at": first_at,
        "end_at": end_at,
        "elapsed_h": (_parse(end_at) - _parse(first_at)).total_seconds() / 3600,
    }
