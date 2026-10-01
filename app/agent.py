"""LLM agent: turns a natural-language question into spatial tool calls.

The model is reached through any OpenAI-compatible endpoint (hosted provider
or a local Ollama server), configured in `app.config`. It can only request the
whitelisted tools of `app.tools`; it never writes or executes code.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from openai import APIConnectionError, APIStatusError, InternalServerError, OpenAI, RateLimitError

from .config import Settings, get_settings
from .tools import TOOL_SCHEMAS, Catalog, run_tool

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a geospatial assistant for Luxembourg. Answer ONLY with facts "
    "returned by the tools; never invent numbers or places. Start by calling "
    "list_layers if you do not know the layer names. Distances are in metres. "
    "If the tools cannot answer the question, say so. "
    "Keep the final answer short and mention which layers you used."
)

RETRYABLE = (RateLimitError, APIConnectionError, InternalServerError)
MAX_BACKOFF_S = 60.0


def build_client(settings: Settings) -> OpenAI:
    """Create the OpenAI-compatible client; retries are handled by `_complete`."""
    return OpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key.get_secret_value(),
        timeout=settings.llm_timeout_s,
        max_retries=0,
    )


def _retry_delay(exc: Exception, attempt: int, base_s: float) -> float:
    """Seconds to wait before retrying: the server's Retry-After, else exponential backoff."""
    if isinstance(exc, APIStatusError):
        try:
            return min(float(exc.response.headers.get("retry-after", "")), MAX_BACKOFF_S)
        except ValueError:
            pass
    return min(base_s * 2**attempt, MAX_BACKOFF_S)


def _complete(client: OpenAI, settings: Settings, messages: list[dict[str, Any]]) -> Any:
    """Call the chat completion endpoint, retrying on rate limits and transient errors."""
    for attempt in range(settings.llm_max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=settings.llm_model, messages=messages, tools=TOOL_SCHEMAS
            )
            return response.choices[0].message
        except RETRYABLE as exc:
            if attempt == settings.llm_max_retries:
                raise
            delay = _retry_delay(exc, attempt, settings.llm_retry_base_s)
            logger.warning(
                "LLM call failed (%s); retry %d/%d in %.1f s",
                type(exc).__name__,
                attempt + 1,
                settings.llm_max_retries,
                delay,
            )
            time.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def _parse_args(raw: str | None) -> dict[str, Any] | None:
    """Decode the JSON arguments of a tool call; None if they are not a JSON object."""
    try:
        args = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return None
    return args if isinstance(args, dict) else None


def ask(
    question: str,
    cat: Catalog,
    history: list[dict[str, str]] | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Run the tool-calling loop and return the answer plus a trace of tool calls.

    Args:
        question: The user's question in natural language.
        cat: Layers the tools operate on.
        history: Previous turns of the conversation, as
            `{"role": "user" | "assistant", "content": str}` messages.
        settings: Configuration override (defaults to the process settings).

    Returns:
        `{"answer": str, "trace": [{"tool", "args", "summary"}], "geojson": dict | None}`
        where `geojson` is the result of the last tool call that produced features.
    """
    settings = settings or get_settings()
    client = build_client(settings)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *(history or []),
        {"role": "user", "content": question},
    ]
    trace: list[dict[str, Any]] = []
    last_geojson: dict[str, Any] | None = None

    for step in range(settings.max_steps):
        reply = _complete(client, settings, messages)

        if not reply.tool_calls:
            logger.info("Answered after %d step(s), %d tool call(s)", step + 1, len(trace))
            return {"answer": reply.content or "", "trace": trace, "geojson": last_geojson}

        messages.append(reply.model_dump(exclude_none=True))
        for call in reply.tool_calls:
            name = call.function.name
            args = _parse_args(call.function.arguments)
            if args is None:
                args, result = {}, {"error": "Tool arguments must be a JSON object."}
            else:
                result = run_tool(cat, name, args)
            logger.info("Tool call %s(%s)", name, args)
            if "error" in result:
                logger.warning("Tool '%s' returned an error: %s", name, result["error"])
            trace.append(
                {"tool": name, "args": args, "summary": result.get("summary", result.get("error"))}
            )
            if "geojson" in result:
                last_geojson = result["geojson"]
            # Send the LLM a compact result (no full geometries) to save context.
            compact = {k: v for k, v in result.items() if k != "geojson"}
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": json.dumps(compact)}
            )

    logger.warning("Stopped after %d steps without a final answer", settings.max_steps)
    return {
        "answer": "Stopped: the question needed too many steps.",
        "trace": trace,
        "geojson": last_geojson,
    }
