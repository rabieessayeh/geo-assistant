"""FastAPI service exposing the geospatial assistant, the raw tools and the map.

Run:  uvicorn app.api:app --reload   (map interface on http://localhost:8000/)
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from openai import OpenAIError, RateLimitError
from pydantic import BaseModel, Field

from .agent import ask
from .config import Settings, configure_logging, get_settings
from .ratelimit import RateLimiter
from .tools import MAX_MAP_FEATURES, TOOLS, Catalog, list_layers, run_tool, to_geojson

logger = logging.getLogger(__name__)

INDEX_PAGE = Path(__file__).parent / "static" / "index.html"
SOURCES_FILE = "SOURCES.json"  # provenance of the layers, written by scripts/fetch_lux_data.py
GLOBAL_KEY = "*"


class Turn(BaseModel):
    """One previous message of the conversation."""

    role: Literal["user", "assistant"]
    content: str = Field(max_length=4000)


class Question(BaseModel):
    """Body of `POST /ask`."""

    question: str = Field(min_length=1, max_length=1000)
    history: list[Turn] = Field(default_factory=list, max_length=20)


class ToolCall(BaseModel):
    """Body of `POST /tools`."""

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class TraceStep(BaseModel):
    """One tool call made while answering a question."""

    tool: str
    args: dict[str, Any]
    summary: str | None = None


class Answer(BaseModel):
    """Response of `POST /ask`."""

    answer: str
    trace: list[TraceStep]
    geojson: dict[str, Any] | None = None


def _load_sources(data_dir: Path) -> dict[str, Any]:
    """Read the provenance file of a data folder; empty if there is none."""
    path = data_dir / SOURCES_FILE
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Could not read %s", path)
        return {}


def _client_ip(request: Request, trust_forwarded_for: bool) -> str:
    """Address used as rate-limit key.

    Behind a reverse proxy every request comes from the proxy, so the first
    entry of X-Forwarded-For is used instead. A client can forge that header,
    which is why the per-client limit is backed by a global one.
    """
    if trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client else "unknown"


def _wait_text(seconds: float) -> str:
    """Human-readable waiting time, rounded up to the minute."""
    minutes = max(1, math.ceil(seconds / 60))
    return f"{minutes} minute{'s' if minutes > 1 else ''}"


def _window_text(seconds: int) -> str:
    """Human-readable rate-limit window: 'hour', '2 hours', '10 minutes'."""
    for unit, size in (("hour", 3600), ("minute", 60), ("second", 1)):
        if seconds % size == 0:
            count = seconds // size
            return unit if count == 1 else f"{count} {unit}s"
    return f"{seconds} seconds"


def create_app(settings: Settings | None = None, catalog: Catalog | None = None) -> FastAPI:
    """Build the application.

    Args:
        settings: Configuration override (defaults to environment / `.env`).
        catalog: Pre-loaded layers; when omitted they are read from
            `settings.data_dir` at startup.
    """
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level)
        app.state.catalog = catalog or Catalog.from_folder(settings.data_dir)
        app.state.sources = _load_sources(settings.data_dir)
        if settings.llm_key_is_placeholder and "localhost" not in settings.llm_base_url:
            logger.warning("LLM_API_KEY is not set: requests to the LLM will be rejected")
        logger.info(
            "Ready: %d layer(s), LLM model '%s' at %s",
            len(app.state.catalog.layers),
            settings.llm_model,
            settings.llm_base_url,
        )
        yield

    app = FastAPI(
        title="Geo Assistant",
        version="0.2.0",
        description="Natural-language access to geospatial data through whitelisted tools.",
        lifespan=lifespan,
    )

    window = settings.ask_rate_window_s
    client_limiter = RateLimiter(settings.ask_rate_limit, window)
    global_limiter = RateLimiter(settings.ask_global_limit, window)

    def cat(request: Request) -> Catalog:
        return request.app.state.catalog

    def check_rate_limit(request: Request) -> None:
        """Count one question; raise 429 with a friendly message when a limit is reached."""
        ip = _client_ip(request, settings.trust_forwarded_for)
        blocked: tuple[float, str] | None = None
        if settings.ask_rate_limit and (wait := client_limiter.retry_after(ip)) > 0:
            blocked = (
                wait,
                f"You have reached the limit of {settings.ask_rate_limit} questions per "
                f"{_window_text(window)} for this public demo.",
            )
        elif settings.ask_global_limit and (wait := global_limiter.retry_after(GLOBAL_KEY)) > 0:
            blocked = (wait, "This public demo has answered its quota of questions for now.")
        if blocked:
            wait, message = blocked
            logger.info("Rate limit reached for %s (retry in %.0f s)", ip, wait)
            raise HTTPException(
                429,
                f"{message} Please try again in about {_wait_text(wait)}. "
                "The tools remain available without limit through POST /tools.",
                headers={"Retry-After": str(math.ceil(wait))},
            )
        if settings.ask_rate_limit:
            client_limiter.hit(ip)
        if settings.ask_global_limit:
            global_limiter.hit(GLOBAL_KEY)

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        """Map interface (keeps the conversation in the browser)."""
        return FileResponse(INDEX_PAGE)

    @app.get("/health")
    def health(request: Request) -> dict[str, Any]:
        """Liveness probe: loaded layers and configured model (no LLM call)."""
        return {"status": "ok", "layers": sorted(cat(request).layers), "model": settings.llm_model}

    @app.get("/sources")
    def sources(request: Request) -> dict[str, Any]:
        """Provenance of the layers (dataset, publisher, licence), when recorded."""
        return request.app.state.sources

    @app.get("/layers")
    def layers(request: Request) -> dict[str, Any]:
        """Describe the available layers."""
        return list_layers(cat(request))["layers"]

    @app.get("/layers/{name}")
    def layer_features(
        request: Request,
        name: str,
        limit: int = Query(default=MAX_MAP_FEATURES, ge=1, le=10_000),
    ) -> dict[str, Any]:
        """Features of one layer as WGS84 GeoJSON (used by the map to display layers)."""
        catalog = cat(request)
        if name not in catalog.layers:
            raise HTTPException(404, f"Unknown layer '{name}'. Available: {sorted(catalog.layers)}")
        return to_geojson(catalog.layers[name], limit=limit)

    @app.post("/tools")
    def call_tool(request: Request, body: ToolCall) -> dict[str, Any]:
        """Call a spatial tool directly (useful for testing without an LLM)."""
        if body.tool not in TOOLS:
            raise HTTPException(404, f"Unknown tool '{body.tool}'. Available: {sorted(TOOLS)}")
        result = run_tool(cat(request), body.tool, body.args)
        if "error" in result:
            raise HTTPException(400, result["error"])
        return result

    @app.post("/ask", response_model=Answer)
    def ask_question(request: Request, body: Question) -> dict[str, Any]:
        """Natural-language question -> LLM -> spatial tools -> grounded answer."""
        check_rate_limit(request)
        try:
            return ask(
                body.question,
                cat(request),
                history=[t.model_dump() for t in body.history],
                settings=settings,
            )
        except RateLimitError as exc:
            # Provider messages can name the account, so they are logged, not returned.
            logger.error("LLM quota exhausted: %s", exc)
            raise HTTPException(
                503,
                "The language model is receiving too many requests right now. "
                "Please try again in a few minutes.",
            ) from exc
        except OpenAIError as exc:
            logger.error("LLM request failed: %s", exc)
            raise HTTPException(
                502, "The language model could not be reached. Please try again later."
            ) from exc

    return app


app = create_app()
