"""FastAPI service exposing the geospatial assistant, the raw tools and the map.

Run:  uvicorn app.api:app --reload   (map interface on http://localhost:8000/)
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from openai import OpenAIError
from pydantic import BaseModel, Field

from .agent import ask
from .config import Settings, configure_logging, get_settings
from .tools import MAX_MAP_FEATURES, TOOLS, Catalog, list_layers, run_tool, to_geojson

logger = logging.getLogger(__name__)

INDEX_PAGE = Path(__file__).parent / "static" / "index.html"


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

    def cat(request: Request) -> Catalog:
        return request.app.state.catalog

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        """Map interface (keeps the conversation in the browser)."""
        return FileResponse(INDEX_PAGE)

    @app.get("/health")
    def health(request: Request) -> dict[str, Any]:
        """Liveness probe: loaded layers and configured model (no LLM call)."""
        return {"status": "ok", "layers": sorted(cat(request).layers), "model": settings.llm_model}

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
        try:
            return ask(
                body.question,
                cat(request),
                history=[t.model_dump() for t in body.history],
                settings=settings,
            )
        except OpenAIError as exc:
            logger.error("LLM request failed: %s", exc)
            raise HTTPException(502, f"LLM error: {exc}") from exc

    return app


app = create_app()
