"""End-to-end tests for the MCP progress bridge over a real FastMCP client<->server.

Includes a regression for the reserved-LogRecord-key bug: the ProgressEvent has a
top-level ``message`` field, and passing the raw event as a logging ``extra`` made
FastMCP's LogRecord construction raise ``KeyError('message')`` — which was silently
swallowed, dropping every structured event. We now nest under ``MCP_EVENT_EXTRA_KEY``.
"""

import pytest
from fastmcp import Client, FastMCP

from hindsight_api.engine.streaming import (
    ProgressOperation,
    ProgressPhase,
    ProgressStatus,
    ReflectIterationData,
    ReflectToolData,
)
from hindsight_api.mcp_progress import MCP_EVENT_EXTRA_KEY, mcp_progress_emitter


def _streaming_server() -> FastMCP:
    mcp = FastMCP("test-streaming")

    @mcp.tool
    async def streamy(query: str) -> dict:
        # Uses the production helper: resolves the active MCP Context and emits.
        emitter = mcp_progress_emitter(ProgressOperation.REFLECT)
        assert emitter is not None, "expected an MCP request context inside the tool"
        await emitter.send(
            phase=ProgressPhase.ITERATION,
            status=ProgressStatus.STARTED,
            message="Iteration 1 of 2",  # top-level `message` — the reserved-key trap
            data=ReflectIterationData(iteration=1, max_iterations=2),
        )
        await emitter.send(
            phase=ProgressPhase.TOOL_CALL,
            status=ProgressStatus.COMPLETED,
            message="recall: 10 chars in 5ms",
            data=ReflectToolData(iteration=1, tool="recall", input_summary="q", output_chars=10, duration_ms=5),
        )
        return {"ok": True}

    return mcp


@pytest.mark.asyncio
async def test_mcp_bridge_streams_progress_and_structured_logs():
    logs: list[dict] = []
    progress: list[tuple] = []

    async def log_handler(message):
        logs.append(message.data)

    async def progress_handler(prog, total, msg):
        progress.append((prog, total, msg))

    client = Client(_streaming_server(), log_handler=log_handler, progress_handler=progress_handler)
    async with client:
        await client.set_logging_level("debug")
        result = await client.call_tool("streamy", {"query": "x"})

    # Progress channel: EVERY event yields a progress notification (regression: this was
    # previously reflect-iteration-only, so tool/response/stage events never showed).
    assert len(progress) == 2
    assert progress[0][2] == "Iteration 1 of 2"

    # Logging channel: the full structured event arrives, nested under the wrapper key.
    # Regression: the top-level `message` field used to raise KeyError in LogRecord and
    # silently drop these — this assertion fails if that bug returns.
    events = [d["extra"][MCP_EVENT_EXTRA_KEY] for d in logs if d.get("extra") and MCP_EVENT_EXTRA_KEY in d["extra"]]
    kinds = [e["data"]["kind"] for e in events]
    assert kinds == ["reflect_iteration", "reflect_tool"]
    assert events[1]["data"]["tool"] == "recall"
    assert events[1]["data"]["duration_ms"] == 5

    # The tool result is still returned whole (MCP tool results are atomic).
    assert result.data == {"ok": True}


@pytest.mark.asyncio
async def test_mcp_progress_emitter_is_none_outside_request():
    # Outside an MCP request there is no context, so streaming is silently skipped
    # and the operation runs exactly as before.
    assert mcp_progress_emitter(ProgressOperation.RECALL) is None
