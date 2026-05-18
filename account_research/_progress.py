"""Progress marker emission for the pipeline.

Writes a single line to stdout per pipeline checkpoint, in a format the
Streamlit UI parses to drive its 7-agent stepper:

    [PROGRESS] agent=<name> status=<state> ts=<iso8601>[ iter=<n>/<m>][ info=<token>]

Statuses: start | done | skipped | failed | halted
"""
from __future__ import annotations

from datetime import datetime, timezone


def emit(
    agent: str,
    status: str,
    *,
    iter: tuple[int, int] | None = None,
    info: str | None = None,
) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    parts = [f"[PROGRESS] agent={agent} status={status} ts={ts}"]
    if iter is not None:
        parts.append(f"iter={iter[0]}/{iter[1]}")
    if info:
        parts.append(f"info={info}")
    print(" ".join(parts), flush=True)
