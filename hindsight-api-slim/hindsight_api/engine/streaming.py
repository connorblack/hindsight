"""Progress-streaming protocol for reflect and recall.

Both ``reflect`` (an LLM agentic loop) and ``recall`` (a deterministic
retrieval DAG) already compute rich intermediate state at clean boundaries but
only return it after the whole operation finishes. This module defines a single
event vocabulary plus a per-request emitter so those boundaries can be surfaced
live to any consumer:

* the control-plane UI, over an HTTP SSE endpoint (drains ``QueueProgressEmitter``);
* downstream MCP clients (OpenWebUI/Hermes), by mapping events onto MCP
  ``notifications/progress`` + ``notifications/message`` in the MCP tools;
* an example FastMCP app used for smoke testing.

The emitter is threaded through the engine exactly like the existing
``cancel_check`` callback — one optional argument, guarded at each call site —
so control flow and the cancellation/cache-teardown guarantees are untouched.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field


class ProgressOperation(StrEnum):
    """Which operation a stream of events describes."""

    RECALL = "recall"
    REFLECT = "reflect"


class ProgressPhase(StrEnum):
    """The stage of work an event reports.

    Recall is a fixed DAG (``retrieval`` -> ``fusion`` -> ``rerank``); reflect is
    an agentic loop that repeats ``iteration`` / ``tool_call`` / ``response``.
    ``final`` and ``error`` are shared terminal phases.
    """

    # Recall DAG stages
    RETRIEVAL = "retrieval"
    FUSION = "fusion"
    RERANK = "rerank"
    # Reflect agentic-loop stages
    ITERATION = "iteration"
    TOOL_CALL = "tool_call"
    RESPONSE = "response"
    # Shared terminal phases
    FINAL = "final"
    ERROR = "error"


class ProgressStatus(StrEnum):
    """Lifecycle marker for a phase — e.g. a tool call emits ``started`` then ``completed``."""

    STARTED = "started"
    COMPLETED = "completed"
    FINAL = "final"
    ERROR = "error"


class RetrievalMethodStat(BaseModel):
    """One cell of the recall "Parallel Retrieval" grid: a (fact_type, method) result."""

    fact_type: str = Field(description="world | experience | observation")
    method: str = Field(description="semantic | bm25 | graph | temporal")
    count: int = Field(description="Number of candidates this method returned for this fact type")
    duration_s: float = Field(description="Wall-clock seconds spent in this method")


class RecallStageData(BaseModel):
    """Payload for a completed recall DAG stage (retrieval / fusion / rerank)."""

    kind: Literal["recall_stage"] = "recall_stage"
    stage: str = Field(description="retrieval | fusion | rerank")
    count: int = Field(description="Headline count for the stage: candidates, fused, or scored")
    duration_s: float = Field(description="Wall-clock seconds spent in this stage")
    # Populated only for the retrieval stage — the per-(fact_type, method) breakdown.
    methods: list[RetrievalMethodStat] = Field(default_factory=list)


class ReflectIterationData(BaseModel):
    """Payload for the start of a reflect agentic-loop iteration."""

    kind: Literal["reflect_iteration"] = "reflect_iteration"
    iteration: int = Field(description="1-based iteration index")
    max_iterations: int


class ReflectToolData(BaseModel):
    """Payload for a reflect tool invocation (search_observations / recall / etc.).

    ``duration_ms`` and ``output_chars`` are populated on the ``completed`` event;
    they are ``None`` on the ``started`` event.
    """

    kind: Literal["reflect_tool"] = "reflect_tool"
    iteration: int = Field(description="1-based iteration index that made this tool call")
    tool: str = Field(description="Tool name, e.g. search_observations, recall")
    input_summary: str = Field(description="Human-readable summary of the tool arguments")
    output_chars: int | None = Field(default=None, description="Size of the tool result, set on completion")
    duration_ms: int | None = Field(default=None, description="Tool wall-clock ms, set on completion")


class ReflectResponseData(BaseModel):
    """Payload carrying the model's response text produced in a reflect iteration."""

    kind: Literal["reflect_response"] = "reflect_response"
    iteration: int = Field(description="1-based iteration index that produced this text")
    text: str = Field(description="The assistant's response/reasoning text for this iteration")


class ProgressErrorData(BaseModel):
    """Payload for a non-fatal or fatal error surfaced mid-stream."""

    kind: Literal["error"] = "error"
    detail: str


# Discriminated union so events round-trip cleanly (SSE frame -> client, MCP extra -> client).
ProgressData = Annotated[
    RecallStageData | ReflectIterationData | ReflectToolData | ReflectResponseData | ProgressErrorData,
    Field(discriminator="kind"),
]


class ProgressEvent(BaseModel):
    """A single ordered progress event for a reflect or recall operation."""

    operation: ProgressOperation
    operation_id: str = Field(description="Correlation id — stable for the lifetime of one operation")
    seq: int = Field(description="Monotonic per-operation sequence number, starting at 1")
    phase: ProgressPhase
    status: ProgressStatus
    ts: datetime = Field(description="UTC timestamp the event was emitted")
    message: str = Field(description="Human-readable text (used verbatim for MCP progress/log messages)")
    data: ProgressData | None = Field(default=None, description="Typed phase-specific payload")


class ProgressEmitter:
    """Per-request event builder + sink. One instance per reflect/recall call.

    Owns the monotonic ``seq`` and the envelope stamping so engine call sites stay
    a single ``await emitter.send(...)``. Delivery is delegated to ``_deliver`` —
    subclasses push to an SSE queue, MCP notifications, etc. The base class is a
    usable no-op sink (``_deliver`` does nothing), which keeps the engine's
    ``if emitter is not None`` guards simple and lets tests pass a bare emitter.
    """

    def __init__(self, operation: ProgressOperation, operation_id: str) -> None:
        self._operation = operation
        self._operation_id = operation_id
        self._seq = 0

    @property
    def operation_id(self) -> str:
        return self._operation_id

    async def send(
        self,
        *,
        phase: ProgressPhase,
        status: ProgressStatus,
        message: str,
        data: ProgressData | None = None,
    ) -> None:
        self._seq += 1
        event = ProgressEvent(
            operation=self._operation,
            operation_id=self._operation_id,
            seq=self._seq,
            phase=phase,
            status=status,
            ts=datetime.now(timezone.utc),
            message=message,
            data=data,
        )
        await self._deliver(event)

    async def _deliver(self, event: ProgressEvent) -> None:
        """Base sink is a no-op; subclasses override to route events somewhere."""


class QueueProgressEmitter(ProgressEmitter):
    """Emitter that pushes events onto an ``asyncio.Queue`` for an SSE drain loop.

    A ``None`` sentinel marks end-of-stream so ``events()`` can terminate.
    """

    _SENTINEL: None = None

    def __init__(self, operation: ProgressOperation, operation_id: str, *, maxsize: int = 512) -> None:
        super().__init__(operation, operation_id)
        self._queue: asyncio.Queue[ProgressEvent | None] = asyncio.Queue(maxsize=maxsize)

    async def _deliver(self, event: ProgressEvent) -> None:
        # Never block the engine on a slow SSE consumer — progress is advisory, and
        # the final result is delivered out-of-band by the endpoint after the engine
        # returns, so dropping intermediate events under backpressure is safe.
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    async def aclose(self) -> None:
        """Signal end-of-stream to any active ``events()`` iterator."""
        await self._queue.put(self._SENTINEL)

    async def events(self) -> AsyncIterator[ProgressEvent]:
        """Yield events until ``aclose()`` pushes the sentinel."""
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event


def format_sse(event_name: str, payload: ProgressEvent | BaseModel) -> str:
    """Serialize a model into a single SSE frame.

    ``model_dump_json`` emits one line (no embedded newlines), so a single
    ``data:`` line is sufficient; the blank line terminates the frame.
    """
    return f"event: {event_name}\ndata: {payload.model_dump_json()}\n\n"
