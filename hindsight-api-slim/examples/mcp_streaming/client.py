"""Example FastMCP client that renders Hindsight reflect/recall progress live.

Registers a `progress_handler` (MCP `notifications/progress`) and a `log_handler`
(MCP `notifications/message`, whose `extra` carries the full structured
`ProgressEvent`). This is exactly what a downstream MCP builder — OpenWebUI/Hermes,
an agent, a custom UI — does to stream Hindsight's reflect/recall internals to its
own callers.

By default it connects to the paired demo server in-process (zero infra) and
asserts the streamed events arrived (a runnable smoke). Pass an MCP URL to attach
to a real Hindsight server instead:

    uv run python examples/mcp_streaming/client.py
    uv run python examples/mcp_streaming/client.py http://127.0.0.1:8123/mcp/
"""

import asyncio
import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.client.logging import LogMessage

from hindsight_api.mcp_progress import MCP_EVENT_EXTRA_KEY

# Allow `import server` when run as a script from anywhere.
sys.path.insert(0, str(Path(__file__).parent))


async def progress_handler(progress: float, total: float | None, message: str | None) -> None:
    bar = f"{(progress / total) * 100:.0f}%" if total else f"{progress}"
    print(f"   ├─ progress {bar} — {message or ''}")


def make_log_handler(collected: list[dict]):
    async def log_handler(message: LogMessage) -> None:
        data = message.data or {}
        event = (data.get("extra") or {}).get(MCP_EVENT_EXTRA_KEY) or {}
        collected.append(event)
        payload = event.get("data") or {}
        kind = payload.get("kind")
        if kind == "reflect_iteration":
            print(f"   ▶  iteration {payload['iteration']}/{payload['max_iterations']}")
        elif kind == "reflect_response":
            print(f"   💭 [{payload['iteration']}] {payload['text']}")
        elif kind == "reflect_tool":
            if event.get("status") == "started":
                print(f"   🔧 [{payload['iteration']}] {payload['tool']}({payload['input_summary']}) …")
            else:
                print(
                    f"   ✅ [{payload['iteration']}] {payload['tool']} → "
                    f"{payload['output_chars']} chars in {payload['duration_ms']}ms"
                )
        elif kind == "recall_stage":
            print(f"   📊 {payload['stage']}: {payload['count']} in {payload['duration_s']}s")
        else:
            print(f"   · {data.get('msg')}")

    return log_handler


def _final(result) -> object:
    # FastMCP CallToolResult exposes structured output as `.data`; fall back to content.
    return (
        getattr(result, "data", None)
        if getattr(result, "data", None) is not None
        else getattr(result, "content", result)
    )


async def main(target: object) -> None:
    collected: list[dict] = []
    client = Client(target, progress_handler=progress_handler, log_handler=make_log_handler(collected))

    async with client:
        # Open the structured log channel: the server only sends notifications/message
        # at/above the level the client sets. The progress channel needs no such setup.
        await client.set_logging_level("debug")

        print("── reflect_demo ──────────────────────────────")
        result = await client.call_tool("reflect_demo", {"query": "articles and headlines I've read"})
        print(f"   ⇒ final: {_final(result)}\n")

        print("── recall_demo ───────────────────────────────")
        result = await client.call_tool("recall_demo", {"query": "bloomberg, FT, forbes"})
        print(f"   ⇒ final: {_final(result)}\n")

    kinds = {(e.get("data") or {}).get("kind") for e in collected}
    expected = {"reflect_iteration", "reflect_response", "reflect_tool", "recall_stage"}
    missing = expected - kinds
    if missing:
        raise SystemExit(f"SMOKE FAILED: missing streamed event kinds {missing} (got {kinds})")
    print(f"SMOKE OK: received {len(collected)} streamed events covering {sorted(kinds)}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target: object = sys.argv[1]  # MCP URL of a real server
    else:
        import server  # in-process demo server (zero infra)

        target = server.mcp
    asyncio.run(main(target))
