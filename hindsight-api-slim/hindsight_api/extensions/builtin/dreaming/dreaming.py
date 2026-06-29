"""Hindsight ``dreaming`` extension — HTTP endpoint + optional MCP tool.

Blessed extension patterns (see hindsight-docs / Extensions):
  * ``HttpExtension.get_router(memory)`` returns a FastAPI router mounted at
    ``/ext/`` — so ``POST /ext/dream`` is the public route.
  * ``MCPExtension.register_tools(mcp, memory)`` is SYNCHRONOUS (the core call
    site does not await it) and registers ``@mcp.tool()`` async functions.
  * The HTTP extension receives the ``MemoryEngine`` via ``get_router`` (it is
    NOT given an ``ExtensionContext`` — the loader passes no context for HTTP),
    so we stash it on ``self`` for the ``on_startup`` background scheduler.

Loadable via:
    HINDSIGHT_API_HTTP_EXTENSION=hindsight_api.extensions.builtin.dreaming:DreamingHttpExtension
    HINDSIGHT_API_MCP_EXTENSION=hindsight_dreaming:DreamingMCPExtension
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from hindsight_api.extensions import HttpExtension, MCPExtension
from hindsight_api.models import RequestContext

from .config import DreamingConfig
from .reduce import (
    ClusterPlan,
    DreamReport,
    InvalidDreamScope,
    ensure_archive_table,
    list_bank_ids,
    reduce_bank,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastmcp import FastMCP

    from hindsight_api import MemoryEngine

logger = logging.getLogger(__name__)


def get_request_context(authorization: str | None = Header(default=None)) -> RequestContext:
    """Extract request auth exactly like the core HTTP routes."""

    api_key = None
    if authorization:
        if authorization.lower().startswith("bearer "):
            api_key = authorization[7:].strip()
        else:
            api_key = authorization.strip()
    return RequestContext(api_key=api_key)


def _get_mcp_request_context() -> RequestContext:
    """Mirror core MCP tool auth propagation for this standalone extension tool."""

    from hindsight_api.api.mcp import (
        get_current_api_key,
        get_current_api_key_id,
        get_current_mcp_authenticated,
        get_current_tenant_id,
    )

    return RequestContext(
        api_key=get_current_api_key(),
        tenant_id=get_current_tenant_id(),
        api_key_id=get_current_api_key_id(),
        mcp_authenticated=get_current_mcp_authenticated(),
    )


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------
class DreamRequest(BaseModel):
    mode: Literal["dry-run", "apply"] = "dry-run"
    bank_id: str
    # A specific tag-set scope. Omit to discover + reduce ALL scopes in the bank.
    # An explicit empty list ([]) targets the untagged ("shared") scope.
    scope: list[str] | None = None
    max_scopes: int | None = Field(default=None, ge=0)
    k: int | None = Field(default=None, ge=1)
    parallelism: int | None = Field(default=None, ge=1)


class MemberOut(BaseModel):
    id: str
    text: str
    occurred_start: str | None = None
    occurred_end: str | None = None
    mentioned_at: str | None = None
    role: str  # survivor | exact-text | semantic | kept
    reason: str = ""


class ClusterOut(BaseModel):
    scope: list[str]
    survivor_id: str
    merged_text: str
    members: list[MemberOut]
    merged_member_ids: list[str]
    applied: bool
    archived_count: int


class ScopeOut(BaseModel):
    scope: list[str]
    observations_scanned: int
    clusters_proposed: int
    observations_merged: int
    error: str | None = None
    clusters: list[ClusterOut]


class DreamResponse(BaseModel):
    mode: str
    bank_id: str
    scopes_scanned: int
    clusters_proposed: int
    observations_merged: int
    scopes: list[ScopeOut]


def _cluster_to_out(plan: ClusterPlan) -> ClusterOut:
    return ClusterOut(
        scope=plan.scope,
        survivor_id=plan.survivor_id,
        merged_text=plan.merged_text,
        members=[
            MemberOut(
                id=m.id,
                text=m.text,
                occurred_start=m.occurred_start,
                occurred_end=m.occurred_end,
                mentioned_at=m.mentioned_at,
                role=m.role,
                reason=m.reason,
            )
            for m in plan.members
        ],
        merged_member_ids=plan.merged_member_ids,
        applied=plan.applied,
        archived_count=plan.archived_count,
    )


def _report_to_response(report: DreamReport) -> DreamResponse:
    return DreamResponse(
        mode=report.mode,
        bank_id=report.bank_id,
        scopes_scanned=report.scopes_scanned,
        clusters_proposed=report.clusters_proposed,
        observations_merged=report.observations_merged,
        scopes=[
            ScopeOut(
                scope=s.scope,
                observations_scanned=s.observations_scanned,
                clusters_proposed=s.clusters_proposed,
                observations_merged=s.observations_merged,
                error=s.error,
                clusters=[_cluster_to_out(c) for c in s.clusters],
            )
            for s in report.scopes
        ],
    )


# ---------------------------------------------------------------------------
# HTTP extension
# ---------------------------------------------------------------------------
class DreamingHttpExtension(HttpExtension):
    """Exposes ``POST /ext/dream`` and runs the optional background scheduler."""

    def __init__(self, config: dict[str, str]):
        super().__init__(config)
        self._dcfg = DreamingConfig.from_env()
        self._memory: "MemoryEngine | None" = None
        self._stop = asyncio.Event()
        self._scheduler_task: asyncio.Task | None = None

    # get_router is called synchronously while the app is built, BEFORE the
    # lifespan startup fires on_startup — so stashing ``memory`` here guarantees
    # it is available to the scheduler.
    def get_router(self, memory: "MemoryEngine") -> APIRouter:
        self._memory = memory
        router = APIRouter(tags=["Dreaming"])

        @router.post("/dream", response_model=DreamResponse)
        async def dream(
            req: DreamRequest,
            request_context: RequestContext = Depends(get_request_context),
        ) -> DreamResponse:
            await memory._authenticate_tenant(request_context)
            if not self._dcfg.enabled:
                raise HTTPException(status_code=403, detail="Dreaming extension is disabled (HINDSIGHT_API_DREAM_ENABLED=false)")
            try:
                report = await reduce_bank(
                    memory,
                    req.bank_id,
                    dcfg=self._dcfg,
                    request_context=request_context,
                    mode=req.mode,
                    scope_override=req.scope,
                    max_scopes=req.max_scopes,
                    k=req.k,
                    parallelism=req.parallelism,
                )
            except InvalidDreamScope as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return _report_to_response(report)

        @router.get("/dream/config")
        async def dream_config() -> dict:
            return {
                "enabled": self._dcfg.enabled,
                "reduce_threshold": self._dcfg.reduce_threshold,
                "interval_seconds": self._dcfg.interval_seconds,
                "k": self._dcfg.k,
                "parallelism": self._dcfg.parallelism,
                "max_scopes": self._dcfg.max_scopes,
            }

        return router

    async def on_startup(self) -> None:
        if not self._dcfg.enabled:
            logger.info("[DREAM] extension disabled (HINDSIGHT_API_DREAM_ENABLED=false); nothing started")
            return
        if self._memory is None:
            # Sub-app mount path (lifespan/on_startup may not fire, or get_router
            # ran without us): the lazy ensure in apply mode is the backstop.
            logger.warning("[DREAM] memory engine not available at startup; archive table created lazily on first apply")
            return

        # Create the archive table at startup (idempotent), per the brief. Wrapped so
        # a DDL failure can never crash the host app; apply-mode reduce re-ensures it.
        try:
            await ensure_archive_table(self._memory)
        except Exception:
            logger.exception("[DREAM] failed to ensure archive table at startup (will retry lazily on apply)")

        if self._dcfg.interval_seconds <= 0:
            logger.info("[DREAM] background scheduler disabled (interval=0); endpoint POST /ext/dream is live")
            return
        self._stop.clear()
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        logger.info("[DREAM] background scheduler started (interval=%ss)", self._dcfg.interval_seconds)

    async def on_shutdown(self) -> None:
        self._stop.set()
        task = self._scheduler_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover - defensive
                logger.exception("[DREAM] scheduler task raised during shutdown")
        self._scheduler_task = None

    async def _scheduler_loop(self) -> None:
        """Low-cadence apply-mode reduce across all banks. Fully decoupled —
        a failure in any iteration is logged and never propagates to the host."""
        interval = self._dcfg.interval_seconds
        memory = self._memory
        assert memory is not None
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                break
            try:
                bank_ids = await list_bank_ids(memory)
                for bank_id in bank_ids:
                    if self._stop.is_set():
                        break
                    try:
                        report = await reduce_bank(memory, bank_id, dcfg=self._dcfg, mode="apply")
                        if report.observations_merged:
                            logger.info(
                                "[DREAM] scheduled reduce bank=%s merged=%d scopes=%d",
                                bank_id,
                                report.observations_merged,
                                report.scopes_scanned,
                            )
                    except Exception:
                        logger.exception("[DREAM] scheduled reduce failed for bank=%s", bank_id)
            except Exception:
                logger.exception("[DREAM] scheduler iteration failed")


# ---------------------------------------------------------------------------
# MCP extension (optional)
# ---------------------------------------------------------------------------
class DreamingMCPExtension(MCPExtension):
    """Registers a ``dream`` MCP tool wrapping the same reduce logic.

    ``register_tools`` is synchronous on purpose: the core MCP loader calls it
    without ``await`` (hindsight_api/api/mcp.py), so an ``async def`` here would
    return an un-awaited coroutine and register nothing.
    """

    def __init__(self, config: dict[str, str]):
        super().__init__(config)
        self._dcfg = DreamingConfig.from_env()

    def register_tools(self, mcp: "FastMCP", memory: "MemoryEngine") -> None:
        dcfg = self._dcfg

        @mcp.tool()
        async def dream(
            bank_id: str,
            mode: Literal["dry-run", "apply"] = "dry-run",
            scope: list[str] | None = None,
            max_scopes: int | None = None,
            k: int | None = None,
            parallelism: int | None = None,
        ) -> dict:
            """Dedup-reduce committed observations within temporal write-scopes.

            mode="dry-run" (default) writes nothing and returns a review report of
            proposed merges; mode="apply" merges LLM-confirmed duplicates, archiving
            redundant rows. Never merges across distinct dates/times/entities.
            """
            request_context = _get_mcp_request_context()
            await memory._authenticate_tenant(request_context)
            if not dcfg.enabled:
                return {"error": "Dreaming extension is disabled (HINDSIGHT_API_DREAM_ENABLED=false)"}
            try:
                report = await reduce_bank(
                    memory,
                    bank_id,
                    dcfg=dcfg,
                    request_context=request_context,
                    mode=mode,
                    scope_override=scope,
                    max_scopes=max_scopes,
                    k=k,
                    parallelism=parallelism,
                )
            except InvalidDreamScope as exc:
                return {"error": str(exc)}
            return _report_to_response(report).model_dump()
