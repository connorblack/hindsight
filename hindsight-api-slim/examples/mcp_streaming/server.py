"""Example FastMCP server demonstrating Hindsight reflect/recall progress streaming.

This server uses the **exact production bridge** — `hindsight_api.mcp_progress`
(`McpProgressEmitter`) and the `hindsight_api.engine.streaming` event protocol —
that Hindsight's real `reflect`/`recall` MCP tools use. The only difference is that
these demo tools emit a *scripted* trace instead of running the engine, so the
example runs with zero infrastructure (no database, no models). Point a client with
the same handlers at the real Hindsight MCP server to see live streams instead.

Run the paired client:

    uv run python examples/mcp_streaming/client.py

or serve it over HTTP for an external MCP client (OpenWebUI/Hermes/etc.):

    uv run python examples/mcp_streaming/server.py   # serves on http://127.0.0.1:8123/mcp/
"""

import asyncio

from fastmcp import FastMCP

from hindsight_api.engine.streaming import (
    ProgressOperation,
    ProgressPhase,
    ProgressStatus,
    RecallStageData,
    ReflectIterationData,
    ReflectResponseData,
    ReflectToolData,
    RetrievalMethodStat,
)
from hindsight_api.mcp_progress import mcp_progress_emitter

mcp = FastMCP("hindsight-streaming-demo")


@mcp.tool
async def reflect_demo(query: str) -> dict:
    """Reflect that streams tool-use + per-iteration responses like the real reflect loop."""
    emitter = mcp_progress_emitter(ProgressOperation.REFLECT)

    # Scripted two-iteration agentic loop: each iteration surfaces the model's
    # reasoning ("response") and the tool it calls, start then completion.
    script = [
        (
            1,
            "Let me search the journal for recent articles.",
            "search_observations",
            "query=recent articles",
            3412,
            812,
        ),
        (2, "Results are sparse — trying a broader recall.", "recall", "query=news headlines 2026", 6714, 2210),
    ]
    if emitter is not None:
        for iteration, reasoning, tool, args, out_chars, dur_ms in script:
            await emitter.send(
                phase=ProgressPhase.ITERATION,
                status=ProgressStatus.STARTED,
                message=f"Iteration {iteration} of {len(script) + 1}",
                data=ReflectIterationData(iteration=iteration, max_iterations=len(script) + 1),
            )
            await emitter.send(
                phase=ProgressPhase.RESPONSE,
                status=ProgressStatus.COMPLETED,
                message="Model reasoning",
                data=ReflectResponseData(iteration=iteration, text=reasoning),
            )
            await emitter.send(
                phase=ProgressPhase.TOOL_CALL,
                status=ProgressStatus.STARTED,
                message=f"{tool}: {args}",
                data=ReflectToolData(iteration=iteration, tool=tool, input_summary=args),
            )
            await asyncio.sleep(0.15)  # simulate tool latency
            await emitter.send(
                phase=ProgressPhase.TOOL_CALL,
                status=ProgressStatus.COMPLETED,
                message=f"{tool}: {out_chars} chars in {dur_ms}ms",
                data=ReflectToolData(
                    iteration=iteration, tool=tool, input_summary=args, output_chars=out_chars, duration_ms=dur_ms
                ),
            )

    return {
        "text": f"Reflection on '{query}': synthesized from recent journal articles.",
        "iterations": len(script) + 1,
    }


@mcp.tool
async def recall_demo(query: str) -> dict:
    """Recall that streams the retrieval -> fusion -> rerank DAG stages."""
    emitter = mcp_progress_emitter(ProgressOperation.RECALL)

    if emitter is not None:
        await emitter.send(
            phase=ProgressPhase.RETRIEVAL,
            status=ProgressStatus.COMPLETED,
            message="Parallel retrieval: 2312 candidates in 12.86s",
            data=RecallStageData(
                stage="retrieval",
                count=2312,
                duration_s=12.86,
                methods=[
                    RetrievalMethodStat(fact_type="world", method="semantic", count=312, duration_s=12.86),
                    RetrievalMethodStat(fact_type="world", method="bm25", count=1000, duration_s=12.86),
                    RetrievalMethodStat(fact_type="world", method="graph", count=1000, duration_s=2.10),
                ],
            ),
        )
        await asyncio.sleep(0.1)
        await emitter.send(
            phase=ProgressPhase.FUSION,
            status=ProgressStatus.COMPLETED,
            message="Fusion: 6208 unique candidates in 0.20s",
            data=RecallStageData(stage="fusion", count=6208, duration_s=0.20),
        )
        await asyncio.sleep(0.1)
        await emitter.send(
            phase=ProgressPhase.RERANK,
            status=ProgressStatus.COMPLETED,
            message="Reranked + scored: 300 results in 8.40s",
            data=RecallStageData(stage="rerank", count=300, duration_s=8.40),
        )

    return {"results": 34, "query": query}


if __name__ == "__main__":
    # Serve over streamable HTTP so any external MCP client can attach.
    mcp.run(transport="http", host="127.0.0.1", port=8123)
