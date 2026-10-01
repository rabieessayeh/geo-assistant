"""Shared fixtures: a tiny in-memory catalogue, offline settings and a scripted fake LLM."""

import json
from types import SimpleNamespace as NS

import geopandas as gpd
import pytest
from shapely.geometry import Point, box

from app.config import Settings
from app.tools import Catalog


@pytest.fixture
def cat() -> Catalog:
    c = Catalog()
    c.add(
        "communes",
        gpd.GeoDataFrame(
            {"name": ["A", "B"]},
            geometry=[box(6.10, 49.60, 6.13, 49.63), box(6.13, 49.60, 6.16, 49.63)],
            crs="EPSG:4326",
        ),
    )
    c.add(
        "stops",
        gpd.GeoDataFrame(
            {"name": ["s1", "s2", "s3"]},
            geometry=[Point(6.11, 49.61), Point(6.12, 49.62), Point(6.15, 49.61)],
            crs="EPSG:4326",
        ),
    )
    c.add(
        "schools",
        gpd.GeoDataFrame(
            {"name": ["near", "far"]},
            geometry=[Point(6.1101, 49.6101), Point(6.155, 49.629)],
            crs="EPSG:4326",
        ),
    )
    return c


@pytest.fixture
def settings() -> Settings:
    """Settings that ignore the developer's `.env` and never wait between retries."""
    return Settings(
        _env_file=None,
        llm_base_url="http://llm.invalid/v1",
        llm_api_key="test-key",
        llm_model="fake-model",
        llm_retry_base_s=0,
    )


class FakeMessage(NS):
    """Mimics the assistant message object of the OpenAI SDK."""

    def model_dump(self, exclude_none: bool = True) -> dict:
        return {
            "role": "assistant",
            "content": self.content,
            "tool_calls": [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.function.name, "arguments": c.function.arguments},
                }
                for c in (self.tool_calls or [])
            ],
        }


def tool_call(name: str, args: dict | str, call_id: str = "c1") -> NS:
    """Build a fake tool call; `args` may be a raw string to simulate malformed JSON."""
    arguments = args if isinstance(args, str) else json.dumps(args)
    return NS(id=call_id, function=NS(name=name, arguments=arguments))


class FakeClient:
    """Scripted LLM: plays `calls` on the first turn, then answers with the tool summary."""

    def __init__(self, calls: list[NS] | None = None, failures: list[Exception] | None = None):
        self.calls = calls or []
        self.failures = failures or []  # exceptions raised before the first success
        self.requests: list[list[dict]] = []
        self.chat = NS(completions=NS(create=self.create))

    def create(self, model: str, messages: list[dict], tools: list[dict]) -> NS:
        if self.failures:
            raise self.failures.pop(0)
        self.requests.append(list(messages))
        if len(self.requests) == 1 and self.calls:
            msg = FakeMessage(content=None, tool_calls=self.calls)
        else:
            last = messages[-1]
            result = json.loads(last["content"]) if last["role"] == "tool" else {}
            text = result.get("summary") or result.get("error") or "No tool was needed."
            msg = FakeMessage(content=text, tool_calls=None)
        return NS(choices=[NS(message=msg)])


@pytest.fixture
def fake_llm(monkeypatch):
    """Return a function installing a `FakeClient` in place of the real LLM client."""

    def install(calls: list[NS] | None = None, failures: list[Exception] | None = None):
        client = FakeClient(calls, failures)
        monkeypatch.setattr("app.agent.build_client", lambda settings: client)
        return client

    return install
