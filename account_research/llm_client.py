"""LLMClient — single wrapper over the Anthropic SDK.

All agent calls go through this client (CLAUDE.md code-style rule). Centralizes
model choice, retries, and JSONL trace logging.

Two preconfigured models:
- SONNET (claude-sonnet-4-6): Disambiguator, Researcher, Estimator, Fact-Checker
- OPUS   (claude-opus-4-7):   Author, Reviewer

Structured output uses Anthropic's forced tool-use pattern: a synthetic tool
named after the target Pydantic schema is registered, the model is forced to
call it, and we parse the tool_use input back into the Pydantic model.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Type, TypeVar

import anthropic
from anthropic import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    RateLimitError,
)
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

SONNET = "claude-sonnet-4-6"
OPUS = "claude-opus-4-7"
HAIKU = "claude-haiku-4-5-20251001"

REPO_ROOT = Path(__file__).resolve().parent.parent
TRACE_DIR = REPO_ROOT / "outputs" / "llm_trace"

T = TypeVar("T", bound=BaseModel)


class LLMOutputError(RuntimeError):
    """Raised when the model's output cannot be validated against the requested schema."""


class LLMClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        max_retries: int = 4,
        initial_backoff_s: float = 1.5,
        trace_dir: Path | None = None,
    ):
        self._client = anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))
        self.max_retries = max_retries
        self.initial_backoff_s = initial_backoff_s
        self.trace_dir = trace_dir or TRACE_DIR
        self.trace_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def complete(
        self,
        *,
        model: str,
        system: str | list[dict],
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: dict | None = None,
        max_tokens: int = 4096,
        temperature: float | None = 0.0,
        agent: str = "unknown",
    ) -> anthropic.types.Message:
        """Raw completion. Returns the SDK Message. Caller handles content blocks.

        ``system`` accepts either a plain string or a list of content blocks
        (e.g. ``[{"type": "text", "text": "...", "cache_control": {"type":
        "ephemeral"}}]``). The block form unlocks prompt caching on the system
        prompt — used by Author / Reviewer to avoid re-billing the static
        instructions on every revision iteration.
        """
        return self._call_with_retry(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            max_tokens=max_tokens,
            temperature=temperature,
            agent=agent,
        )

    def complete_with_json(
        self,
        *,
        model: str,
        system: str | list[dict],
        messages: list[dict],
        schema: Type[T],
        schema_description: str | None = None,
        extra_tools: Iterable[dict] | None = None,
        max_tokens: int = 4096,
        temperature: float | None = 0.0,
        agent: str = "unknown",
    ) -> T:
        """Force the model to emit a value matching `schema` via tool use.

        Pattern: register a single synthetic tool whose input_schema *is* the
        Pydantic schema, then `tool_choice = {"type": "tool", "name": <name>}`
        forces the model to call it. We parse the ToolUseBlock.input back
        into the Pydantic model.

        Pass `extra_tools` to allow Anthropic's server-side tools (web_search,
        web_fetch) to fire before the model is forced into the final answer.
        Note: with extra_tools present we cannot strictly force the schema
        tool, so we use `tool_choice = {"type": "auto"}` and accept whichever
        tool the model picks last; if the last block isn't our schema tool,
        we raise LLMOutputError.
        """
        tool_name = f"emit_{schema.__name__.lower()}"
        schema_tool = {
            "name": tool_name,
            "description": schema_description
            or f"Emit a {schema.__name__} record. Call exactly once with the final structured output.",
            "input_schema": _pydantic_to_tool_schema(schema),
        }

        extras = list(extra_tools or [])
        tools = extras + [schema_tool]

        if extras:
            tool_choice: dict = {"type": "auto"}
        else:
            tool_choice = {"type": "tool", "name": tool_name}

        msg = self._call_with_retry(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            max_tokens=max_tokens,
            temperature=temperature,
            agent=agent,
        )

        tool_use_block = _find_tool_use(msg, tool_name)
        if tool_use_block is None:
            raise LLMOutputError(
                f"Model did not call the {tool_name!r} tool; got stop_reason={msg.stop_reason!r}"
            )

        input_dict = _unwrap_tool_input(tool_use_block.input, schema.__name__)

        try:
            return schema.model_validate(input_dict)
        except ValidationError as exc:
            # ----- Retry #1: truncate string fields that exceed max_length.
            # Opus and Sonnet occasionally write over the cap (e.g.
            # quick_take.body > 600 chars). Truncating with a "…" marker
            # preserves all citations and respects the visual budget.
            truncated, truncated_paths = _truncate_overlong_strings(input_dict, exc)
            if truncated_paths:
                logger.warning(
                    "LLM output validation: truncating %d overlong string "
                    "field(s) and retrying: %s",
                    len(truncated_paths),
                    ", ".join(".".join(str(p) for p in path) for path in truncated_paths[:5]),
                )
                try:
                    return schema.model_validate(truncated)
                except ValidationError as exc_after_trunc:
                    # Fall through to JSON-decode retry below with the
                    # truncated dict as the base.
                    input_dict = truncated
                    exc = exc_after_trunc

            # ----- Retry #2: parse JSON-encoded string sub-structures.
            # Claude sometimes JSON-encodes list/dict values as raw strings
            # (e.g. items=' [{"id": ...}]' instead of items=[{"id": ...}]).
            fixed = _decode_jsonish_strings(input_dict)
            changed = fixed != input_dict
            logger.warning(
                "LLM output validation failed; string-decode retry %s",
                "changed input" if changed else "no-op",
            )
            if changed:
                try:
                    return schema.model_validate(fixed)
                except ValidationError as exc2:
                    raise LLMOutputError(
                        f"Model output failed {schema.__name__} validation "
                        f"(after string-decode retry): {exc2}"
                    ) from exc2
            # Last resort: dump the raw input to disk for inspection
            try:
                from pathlib import Path
                dump = TRACE_DIR / f"failed_input_{uuid.uuid4().hex[:8]}.json"
                dump.write_text(json.dumps(input_dict, default=str, indent=2)[:50000], encoding="utf-8")
                logger.warning("Raw input dumped to %s for inspection", dump)
            except Exception:  # noqa: BLE001
                pass
            raise LLMOutputError(
                f"Model output failed {schema.__name__} validation: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _call_with_retry(
        self,
        *,
        model: str,
        system: str | list[dict],
        messages: list[dict],
        tools: list[dict] | None,
        tool_choice: dict | None,
        max_tokens: int,
        temperature: float | None,
        agent: str,
    ) -> anthropic.types.Message:
        call_id = str(uuid.uuid4())
        start_ns = time.perf_counter_ns()

        kwargs: dict[str, Any] = dict(
            model=model,
            system=system,
            messages=messages,
            max_tokens=max_tokens,
        )
        # Opus 4.7 (and other reasoning models) don't accept `temperature`.
        # Anthropic uses its default when omitted, which is fine for our needs.
        if temperature is not None and not model.startswith("claude-opus-4-7"):
            kwargs["temperature"] = temperature
        if tools:
            kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice

        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                response = self._client.messages.create(**kwargs)
                elapsed_ms = (time.perf_counter_ns() - start_ns) / 1_000_000
                # Anthropic returns cache_read / cache_creation token counts
                # only when cache_control blocks are present. Default to 0
                # when the field is absent (older SDKs) or None.
                cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0
                cache_creation = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
                self._write_trace(
                    call_id=call_id,
                    agent=agent,
                    model=model,
                    attempt=attempt,
                    elapsed_ms=elapsed_ms,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    cache_read_input_tokens=cache_read,
                    cache_creation_input_tokens=cache_creation,
                    stop_reason=response.stop_reason,
                    error=None,
                )
                return response
            except (RateLimitError, APITimeoutError, APIConnectionError) as exc:
                last_exc = exc
            except APIStatusError as exc:
                if exc.status_code >= 500:
                    last_exc = exc
                else:
                    self._write_trace(
                        call_id=call_id,
                        agent=agent,
                        model=model,
                        attempt=attempt,
                        elapsed_ms=(time.perf_counter_ns() - start_ns) / 1_000_000,
                        input_tokens=0,
                        output_tokens=0,
                        cache_read_input_tokens=0,
                        cache_creation_input_tokens=0,
                        stop_reason=None,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    raise

            backoff = self.initial_backoff_s * (2**attempt)
            logger.warning(
                "LLM call retrying in %.1fs (attempt %d/%d, agent=%s): %s",
                backoff,
                attempt + 1,
                self.max_retries,
                agent,
                last_exc,
            )
            time.sleep(backoff)

        self._write_trace(
            call_id=call_id,
            agent=agent,
            model=model,
            attempt=self.max_retries,
            elapsed_ms=(time.perf_counter_ns() - start_ns) / 1_000_000,
            input_tokens=0,
            output_tokens=0,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            stop_reason=None,
            error=f"exhausted retries: {last_exc}",
        )
        assert last_exc is not None
        raise last_exc

    def _write_trace(
        self,
        *,
        call_id: str,
        agent: str,
        model: str,
        attempt: int,
        elapsed_ms: float,
        input_tokens: int,
        output_tokens: int,
        cache_read_input_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
        stop_reason: str | None,
        error: str | None,
    ) -> None:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = self.trace_dir / f"{date}.jsonl"
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "call_id": call_id,
            "agent": agent,
            "model": model,
            "attempt": attempt,
            "elapsed_ms": round(elapsed_ms, 1),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read_input_tokens,
            "cache_creation_input_tokens": cache_creation_input_tokens,
            "stop_reason": stop_reason,
            "error": error,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pydantic_to_tool_schema(model: Type[BaseModel]) -> dict[str, Any]:
    """Convert a Pydantic model's JSON schema into an Anthropic-friendly tool input_schema.

    Anthropic accepts standard JSON Schema. We inline $defs/$ref so the schema
    is self-contained, which a few tool runtimes prefer.
    """
    schema = model.model_json_schema()
    return _inline_refs(schema)


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    defs = schema.pop("$defs", None) or schema.pop("definitions", None) or {}

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node and node["$ref"].startswith("#/"):
                ref_path = node["$ref"].split("/")[-1]
                target = defs.get(ref_path, {})
                merged = {**resolve(target)}
                # Allow sibling keys (e.g. title) to override
                for k, v in node.items():
                    if k != "$ref":
                        merged[k] = resolve(v)
                return merged
            return {k: resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(x) for x in node]
        return node

    return resolve(schema)


def _find_tool_use(msg: anthropic.types.Message, tool_name: str):
    """Return the last ToolUseBlock matching tool_name, or None."""
    found = None
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == tool_name:
            found = block
    return found


# Generic single-key wrapper keys we've observed Claude emit around tool_use
# input. None of these are real fields on any of our schemas, so unwrapping
# them is always safe when the value is itself a dict.
_GENERIC_WRAP_KEYS = (
    "output", "result", "data",
    "parameter", "parameters",
    "input", "value", "payload",
)


def _truncate_overlong_strings(
    input_dict: Any,
    exc: ValidationError,
) -> tuple[Any, list[tuple]]:
    """Walk a ValidationError's `string_too_long` errors and truncate the
    offending fields to their max_length (with a U+2026 marker).

    Returns ``(new_dict, truncated_paths)``. ``new_dict`` is a deep-copied
    mutation of ``input_dict``; ``truncated_paths`` lists the dotted
    locations that were trimmed (empty if no string_too_long errors).

    Used as the first defensive retry path in ``complete_with_json``:
    LLMs sometimes overshoot `max_length` caps by a handful of characters,
    especially on prose fields like ``quick_take.body``. Truncating with a
    visible ellipsis preserves citations + intent while respecting the
    visual budget the schema enforces.
    """
    if not isinstance(input_dict, dict):
        return input_dict, []

    truncated_paths: list[tuple] = []
    out = json.loads(json.dumps(input_dict, default=str))  # deep copy

    for err in exc.errors():
        if err.get("type") != "string_too_long":
            continue
        ctx = err.get("ctx") or {}
        max_len = ctx.get("max_length")
        if not isinstance(max_len, int) or max_len <= 0:
            continue
        path = err.get("loc") or ()
        if not path:
            continue

        # Walk to the parent container; the last path element is the key.
        parent = out
        try:
            for step in path[:-1]:
                if isinstance(parent, list):
                    parent = parent[int(step)]
                else:
                    parent = parent[step]
        except (KeyError, IndexError, TypeError, ValueError):
            continue

        last = path[-1]
        try:
            cur = parent[int(last)] if isinstance(parent, list) else parent[last]
        except (KeyError, IndexError, TypeError, ValueError):
            continue

        if not isinstance(cur, str) or len(cur) <= max_len:
            continue

        # Cut to max_len with an ellipsis marker — cap-1 so the final char
        # is the ellipsis and total length is exactly max_len.
        trimmed = cur[: max(max_len - 1, 0)].rstrip() + "…"
        if isinstance(parent, list):
            parent[int(last)] = trimmed
        else:
            parent[last] = trimmed
        truncated_paths.append(tuple(path))

    return out, truncated_paths


def _unwrap_tool_input(input_dict: Any, schema_name: str) -> Any:
    """Strip the spurious wrappers Claude sometimes adds around tool_use input.

    Three patterns observed in the wild:

    1. ``{"$PARAMETER_NAME": {<actual>}}`` or ``{"$PARAMETER_VALUE": {...}}``
       — leaked template placeholders from the JSON Schema definition.
    2. ``{"<schema-derived>": {<actual>}}`` — e.g. ``{"brief": {...}}`` for
       ``BriefData``, ``{"report": {...}}`` for ``ReviewerReport``.
    3. ``{"parameter"|"input"|"output"|...: {<actual>}}`` — a generic wrapper
       borrowed from the tool_use schema vocabulary.

    Returns the unwrapped dict, or the original value if no pattern matched.
    """
    # Pattern 1: literal $PARAMETER_NAME / $PARAMETER_VALUE placeholders
    for placeholder in ("$PARAMETER_NAME", "$PARAMETER_VALUE"):
        if (isinstance(input_dict, dict)
                and len(input_dict) == 1
                and placeholder in input_dict
                and isinstance(input_dict[placeholder], dict)):
            input_dict = input_dict[placeholder]
            break

    # Patterns 2 + 3: single-key dict whose key is either schema-derived or
    # in the generic wrap list, and whose value is itself a dict.
    if (isinstance(input_dict, dict)
            and len(input_dict) == 1
            and isinstance(next(iter(input_dict.values())), dict)):
        sole_key = next(iter(input_dict.keys())).lower()
        schema_lc = schema_name.lower()
        if (sole_key in schema_lc
                or schema_lc.startswith(sole_key)
                or sole_key in _GENERIC_WRAP_KEYS):
            input_dict = next(iter(input_dict.values()))

    return input_dict


def _decode_jsonish_strings(node: Any) -> Any:
    """Recursively walk a dict/list and parse any string that LOOKS like JSON.

    Claude sometimes emits sub-structures as JSON-encoded strings instead of
    native objects (e.g. items='[{"id": ...}]'). Worse: it occasionally
    produces malformed JSON inside that string (missing commas, etc.). We
    try strict json.loads first; on failure, fall back to json_repair which
    fixes common LLM JSON errors.
    """
    if isinstance(node, dict):
        return {k: _decode_jsonish_strings(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_decode_jsonish_strings(v) for v in node]
    if isinstance(node, str):
        stripped = node.lstrip()
        if stripped[:1] in ("[", "{"):
            try:
                parsed = json.loads(stripped)
                return _decode_jsonish_strings(parsed)
            except (ValueError, json.JSONDecodeError):
                pass
            # Strict parse failed — try forgiving repair
            try:
                from json_repair import repair_json
                parsed = repair_json(stripped, return_objects=True)
                if parsed:  # repair_json returns "" on total failure
                    return _decode_jsonish_strings(parsed)
            except Exception:  # noqa: BLE001
                pass
    return node
