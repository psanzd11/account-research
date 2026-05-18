"""Tests for the [PROGRESS] marker emitter and the UI-side parser.

Keeps the format stable: the UI in `pages/2_Run.py` and the producers
in `cli.py` / `orchestrator.py` agree on this exact string shape.
"""
from __future__ import annotations

import re

import pytest

from account_research._progress import emit


PROGRESS_RE = re.compile(
    r"^\[PROGRESS\] agent=(?P<agent>\w+) status=(?P<status>\w+) "
    r"ts=(?P<ts>\S+)"
    r"(?: iter=(?P<iter>\d+)/(?P<max>\d+))?"
    r"(?: info=(?P<info>\S+))?$"
)


def _last_line(captured: pytest.CaptureFixture[str]) -> str:
    out = captured.readouterr().out.strip().splitlines()
    assert out, "no stdout captured"
    return out[-1]


def test_basic_start(capsys: pytest.CaptureFixture[str]) -> None:
    emit("researcher", "start")
    line = _last_line(capsys)
    m = PROGRESS_RE.match(line)
    assert m, f"line did not match: {line!r}"
    assert m.group("agent") == "researcher"
    assert m.group("status") == "start"
    assert m.group("iter") is None
    assert m.group("info") is None


def test_with_iter(capsys: pytest.CaptureFixture[str]) -> None:
    emit("author", "done", iter=(2, 3))
    m = PROGRESS_RE.match(_last_line(capsys))
    assert m
    assert m.group("agent") == "author"
    assert m.group("status") == "done"
    assert m.group("iter") == "2"
    assert m.group("max") == "3"


def test_with_info(capsys: pytest.CaptureFixture[str]) -> None:
    emit("disambiguator", "halted", info="ambiguous")
    m = PROGRESS_RE.match(_last_line(capsys))
    assert m
    assert m.group("status") == "halted"
    assert m.group("info") == "ambiguous"
    assert m.group("iter") is None


def test_with_iter_and_info(capsys: pytest.CaptureFixture[str]) -> None:
    emit("reviewer", "done", iter=(1, 3), info="revise")
    m = PROGRESS_RE.match(_last_line(capsys))
    assert m
    assert m.group("agent") == "reviewer"
    assert m.group("status") == "done"
    assert m.group("iter") == "1"
    assert m.group("max") == "3"
    assert m.group("info") == "revise"


def test_skipped_no_iter_no_info(capsys: pytest.CaptureFixture[str]) -> None:
    emit("fact_checker", "skipped")
    m = PROGRESS_RE.match(_last_line(capsys))
    assert m
    assert m.group("status") == "skipped"
    assert m.group("info") is None


def test_iso_timestamp_parses(capsys: pytest.CaptureFixture[str]) -> None:
    """The ts field must be parseable by datetime.fromisoformat (used by the UI)."""
    from datetime import datetime

    emit("estimator", "start")
    m = PROGRESS_RE.match(_last_line(capsys))
    assert m
    # Must not raise.
    parsed = datetime.fromisoformat(m.group("ts"))
    assert parsed.tzinfo is not None  # we emit UTC


def test_all_statuses_match(capsys: pytest.CaptureFixture[str]) -> None:
    """Every status the producers emit must be recognised by the regex."""
    for status in ("start", "done", "skipped", "failed", "halted"):
        emit("estimator", status)
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 5
    for line in lines:
        assert PROGRESS_RE.match(line), f"unmatched: {line!r}"
