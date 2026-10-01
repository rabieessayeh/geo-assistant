"""The tool-calling loop, driven by a fake LLM (no server, no API key)."""

import httpx
import pytest
from openai import AuthenticationError, RateLimitError

from app import agent
from tests.conftest import tool_call

WITHIN = {"target": "schools", "reference": "stops", "meters": 200}


def _api_error(cls: type, status: int, headers: dict | None = None) -> Exception:
    request = httpx.Request("POST", "http://llm.invalid/v1/chat/completions")
    response = httpx.Response(status, request=request, headers=headers)
    return cls("simulated", response=response, body=None)


def test_agent_loop(cat, settings, fake_llm):
    fake_llm([tool_call("within_distance", WITHIN)])
    out = agent.ask("Which schools are within 200 m of a stop?", cat, settings=settings)
    assert out["trace"] == [
        {
            "tool": "within_distance",
            "args": WITHIN,
            "summary": "1 of 2 'schools' features are within 200 m of 'stops'.",
        }
    ]
    assert "1 of 2" in out["answer"]
    assert out["geojson"]["features"][0]["properties"]["name"] == "near"


def test_llm_never_receives_geometries(cat, settings, fake_llm):
    client = fake_llm([tool_call("within_distance", WITHIN)])
    agent.ask("Which schools are within 200 m of a stop?", cat, settings=settings)
    tool_message = client.requests[1][-1]
    assert tool_message["role"] == "tool"
    assert "geojson" not in tool_message["content"]
    assert "coordinates" not in tool_message["content"]


def test_agent_history(cat, settings, fake_llm):
    """Previous turns are sent to the LLM between the system prompt and the question."""
    client = fake_llm([tool_call("within_distance", WITHIN)])
    history = [
        {"role": "user", "content": "Which layers exist?"},
        {"role": "assistant", "content": "schools and stops."},
    ]
    agent.ask("And within 200 m?", cat, history=history, settings=settings)
    assert [m["role"] for m in client.requests[0]] == ["system", "user", "assistant", "user"]


def test_unknown_tool_is_reported_to_the_llm(cat, settings, fake_llm):
    fake_llm([tool_call("run_python", {"code": "import os"})])
    out = agent.ask("Delete everything", cat, settings=settings)
    assert out["trace"][0]["tool"] == "run_python"
    assert "Unknown tool" in out["trace"][0]["summary"]
    assert out["geojson"] is None


@pytest.mark.parametrize("raw", ["{not json", "[1, 2]", '"text"'])
def test_malformed_arguments_do_not_crash(cat, settings, fake_llm, raw):
    fake_llm([tool_call("within_distance", raw)])
    out = agent.ask("Which schools are near a stop?", cat, settings=settings)
    assert out["trace"][0]["summary"] == "Tool arguments must be a JSON object."


def test_stops_after_max_steps(cat, settings, fake_llm):
    client = fake_llm([tool_call("list_layers", {})])
    client.requests = _AlwaysFirst()  # the fake keeps asking for the same tool
    out = agent.ask("Loop forever", cat, settings=settings.model_copy(update={"max_steps": 3}))
    assert out["answer"].startswith("Stopped")
    assert len(out["trace"]) == 3


class _AlwaysFirst(list):
    """A request log that always looks like the first turn to `FakeClient`."""

    def __len__(self) -> int:
        return 1


def test_retries_on_rate_limit(cat, settings, fake_llm, monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr(agent.time, "sleep", waits.append)
    failures = [
        _api_error(RateLimitError, 429, {"retry-after": "7"}),
        _api_error(RateLimitError, 429),
    ]
    fake_llm([tool_call("within_distance", WITHIN)], failures=failures)
    slow = settings.model_copy(update={"llm_retry_base_s": 2.0})
    out = agent.ask("Which schools are within 200 m of a stop?", cat, settings=slow)
    assert "1 of 2" in out["answer"]
    # First wait honours Retry-After, the second one is the exponential backoff (2 * 2**1).
    assert waits == [7.0, 4.0]


def test_gives_up_after_max_retries(cat, settings, fake_llm, monkeypatch):
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    fake_llm(failures=[_api_error(RateLimitError, 429) for _ in range(10)])
    with pytest.raises(RateLimitError):
        agent.ask("Anything", cat, settings=settings.model_copy(update={"llm_max_retries": 2}))


def test_does_not_retry_on_bad_credentials(cat, settings, fake_llm, monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr(agent.time, "sleep", waits.append)
    fake_llm(failures=[_api_error(AuthenticationError, 401)])
    with pytest.raises(AuthenticationError):
        agent.ask("Anything", cat, settings=settings)
    assert waits == []
