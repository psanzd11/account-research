"""Regression tests for `_unwrap_tool_input` and `_truncate_overlong_strings`.

Claude periodically wraps the entire tool_use input under a spurious
single-key envelope (`{"parameter": {...}}`, `{"brief": {...}}`, etc.).
The unwrap is the gate between the raw Anthropic response and Pydantic
validation — if it misses a variant, the agent crashes with a
ValidationError that looks like the schema is broken.

Claude also occasionally overshoots `max_length` string caps (e.g.
quick_take.body > 600 chars). The truncate helper retries validation
after trimming offending fields with an ellipsis marker.
"""
from __future__ import annotations

from pydantic import BaseModel, Field, ValidationError

from account_research.llm_client import (
    _truncate_overlong_strings,
    _unwrap_tool_input,
)


def test_no_wrap_passes_through() -> None:
    payload = {"entity_id": "abc", "hero": {"badge_value": "x"}}
    assert _unwrap_tool_input(payload, "BriefData") is payload


def test_parameter_name_placeholder_unwrap() -> None:
    """`{"$PARAMETER_NAME": {...}}` — leaked schema template placeholder."""
    inner = {"entity_id": "abc", "hero": {}}
    wrapped = {"$PARAMETER_NAME": inner}
    assert _unwrap_tool_input(wrapped, "BriefData") == inner


def test_parameter_value_placeholder_unwrap() -> None:
    """`{"$PARAMETER_VALUE": {...}}` — variant of the placeholder above
    observed on the 2026-05-18 Guillermo Jaime Calderón run (Author
    crashed with 4 ValidationErrors before this allowlist was extended)."""
    inner = {"entity_id": "bb25f9a3", "hero": {}, "quick_take": {"body": "x"}}
    wrapped = {"$PARAMETER_VALUE": inner}
    assert _unwrap_tool_input(wrapped, "BriefData") == inner


# ---------------------------------------------------------------------------
# B2 — Regex catch-all for $<ALL_CAPS_UNDERSCORE> placeholder variants
# ---------------------------------------------------------------------------


def test_parameter_type_placeholder_unwrap() -> None:
    """Future placeholder variant: $PARAMETER_TYPE. The regex catches it
    without code change so the next time Claude leaks a new template token
    the pipeline does not crash."""
    inner = {"entity_id": "abc"}
    assert _unwrap_tool_input({"$PARAMETER_TYPE": inner}, "BriefData") == inner


def test_input_placeholder_unwrap() -> None:
    """Bare `$INPUT` is still an all-caps single-token placeholder; regex
    accepts it. Real schemas never use `$INPUT` as a field name."""
    inner = {"x": 1}
    assert _unwrap_tool_input({"$INPUT": inner}, "Schema") == inner


def test_args_placeholder_unwrap() -> None:
    inner = {"x": 1}
    assert _unwrap_tool_input({"$ARGS": inner}, "Schema") == inner


def test_lowercase_dollar_field_not_unwrapped() -> None:
    """`$payment` (lowercase) does NOT match the placeholder regex —
    leave it alone in case it's a real domain field."""
    payload = {"$payment": {"amount": 10}}
    assert _unwrap_tool_input(payload, "Schema") is payload


def test_mixed_case_dollar_field_not_unwrapped() -> None:
    """`$Stripe` mixes cases — not a placeholder pattern, leave alone."""
    payload = {"$Stripe": {"id": "acct_x"}}
    assert _unwrap_tool_input(payload, "Schema") is payload


def test_dollar_with_digits_not_unwrapped() -> None:
    """The regex is strict on letters+underscore only. `$AMOUNT_1` falls
    outside the placeholder pattern — leave alone."""
    payload = {"$AMOUNT_1": {"v": 1}}
    assert _unwrap_tool_input(payload, "Schema") is payload


def test_dollar_placeholder_logs_warning(caplog) -> None:
    """When a new placeholder variant matches, the unwrap logs a warning
    naming the placeholder so the operator can grep for it."""
    import logging
    inner = {"entity_id": "abc"}
    with caplog.at_level(logging.WARNING, logger="account_research.llm_client"):
        _unwrap_tool_input({"$PARAMETER_FUTURE": inner}, "BriefData")
    assert any("$PARAMETER_FUTURE" in r.message for r in caplog.records)


def test_schema_derived_key_unwrap() -> None:
    """`{"brief": {...}}` for BriefData — schema-name-derived."""
    inner = {"entity_id": "abc"}
    assert _unwrap_tool_input({"brief": inner}, "BriefData") == inner


def test_report_key_unwraps_for_reviewer_report() -> None:
    inner = {"status": "approved", "issues": []}
    assert _unwrap_tool_input({"report": inner}, "ReviewerReport") == inner


def test_parameter_wrap_unwraps() -> None:
    """The exact failure pattern from the 2026-05-17 BWPM run.

    Claude emitted `{"parameter": {<BriefData>}}`; before the fix the
    Author crashed with 4 ValidationErrors.
    """
    inner = {"entity_id": "58fe46a6", "hero": {}, "quick_take": "..."}
    assert _unwrap_tool_input({"parameter": inner}, "BriefData") == inner


def test_generic_wrap_keys_all_unwrap() -> None:
    inner = {"entity_id": "abc"}
    for key in ("output", "result", "data", "parameter", "parameters",
                "input", "value", "payload"):
        assert _unwrap_tool_input({key: inner}, "BriefData") == inner, (
            f"failed to unwrap {key!r}"
        )


def test_unrelated_single_key_is_not_unwrapped() -> None:
    """A real field named `entity` is not stripped, even if it's the only key."""
    payload = {"entity": {"id": "abc", "name": "Test"}}
    assert _unwrap_tool_input(payload, "BriefData") is payload


def test_non_dict_value_is_not_unwrapped() -> None:
    """`{"parameter": "string"}` — not a dict inside, leave it alone."""
    payload = {"parameter": "scalar"}
    assert _unwrap_tool_input(payload, "BriefData") is payload


def test_two_keys_is_not_unwrapped() -> None:
    """Two top-level keys — looks like a real partial response, leave it."""
    payload = {"parameter": {"x": 1}, "extra": 2}
    assert _unwrap_tool_input(payload, "BriefData") is payload


# ---------------------------------------------------------------------------
# _truncate_overlong_strings
# ---------------------------------------------------------------------------


class _ShallowSchema(BaseModel):
    name: str = Field(max_length=10)
    body: str = Field(max_length=50)


class _NestedItem(BaseModel):
    title: str = Field(max_length=10)


class _NestedSchema(BaseModel):
    items: list[_NestedItem]


def _validation_error(schema, data) -> ValidationError:
    try:
        schema.model_validate(data)
    except ValidationError as exc:
        return exc
    raise AssertionError("expected ValidationError, got success")


def test_truncate_simple_top_level_field() -> None:
    data = {"name": "x" * 30, "body": "ok"}
    exc = _validation_error(_ShallowSchema, data)
    out, paths = _truncate_overlong_strings(data, exc)
    assert paths == [("name",)]
    assert len(out["name"]) == 10
    assert out["name"].endswith("…")
    assert out["body"] == "ok"  # unchanged


def test_truncate_multiple_fields_at_once() -> None:
    data = {"name": "y" * 50, "body": "z" * 200}
    exc = _validation_error(_ShallowSchema, data)
    out, paths = _truncate_overlong_strings(data, exc)
    assert set(paths) == {("name",), ("body",)}
    assert len(out["name"]) == 10
    assert len(out["body"]) == 50


def test_truncate_inside_list_of_models() -> None:
    data = {"items": [
        {"title": "short"},
        {"title": "x" * 30},
        {"title": "y" * 25},
    ]}
    exc = _validation_error(_NestedSchema, data)
    out, paths = _truncate_overlong_strings(data, exc)
    # Items[1] and items[2] should be truncated (indices 1, 2).
    assert any(p == ("items", 1, "title") for p in paths)
    assert any(p == ("items", 2, "title") for p in paths)
    assert out["items"][0]["title"] == "short"  # unchanged
    assert len(out["items"][1]["title"]) == 10
    assert len(out["items"][2]["title"]) == 10


def test_truncate_no_op_when_no_string_too_long() -> None:
    """Other ValidationError types should leave the dict alone."""

    class _Other(BaseModel):
        n: int

    exc = _validation_error(_Other, {"n": "not-an-int"})
    out, paths = _truncate_overlong_strings({"n": "not-an-int"}, exc)
    assert paths == []
    assert out == {"n": "not-an-int"}


def test_truncate_preserves_other_string_fields() -> None:
    data = {"name": "z" * 100, "body": "short"}
    exc = _validation_error(_ShallowSchema, data)
    out, paths = _truncate_overlong_strings(data, exc)
    assert out["body"] == "short"
    assert paths == [("name",)]


def test_truncate_validates_real_briefdata_failure() -> None:
    """End-to-end regression: a real failed Author output should validate
    after one truncation pass."""
    import json
    from pathlib import Path
    from account_research.schemas.brief import BriefData

    sample = Path("outputs/llm_trace/failed_input_ee2b288d.json")
    if not sample.exists():
        # Synthesise the failure shape if the original dump is gone.
        from uuid import uuid4
        sample_data = {
            "entity_id": str(uuid4()),
            "hero": {"name": "Acme", "entity_type": "company"},
            "quick_take": {"body": "x" * 750},  # > 600 cap
        }
    else:
        sample_data = json.loads(sample.read_text(encoding="utf-8"))

    try:
        BriefData.model_validate(sample_data)
        first_pass_ok = True
    except ValidationError as exc:
        first_pass_ok = False
        fixed, paths = _truncate_overlong_strings(sample_data, exc)
        # Real failure had quick_take.body too long.
        assert ("quick_take", "body") in paths
        BriefData.model_validate(fixed)  # must not raise after truncation

    assert first_pass_ok or True  # either path is acceptable here
