# Streaming reflect & recall over MCP

A runnable example showing how a downstream MCP client (OpenWebUI/Hermes, an agent,
a custom UI) streams Hindsight's **reflect** and **recall** internals live — the
reflect loop's tool-use + per-iteration responses, and the recall retrieval→fusion→rerank
DAG stages — instead of waiting on one opaque tool result.

It uses the **exact production bridge** (`hindsight_api.mcp_progress.McpProgressEmitter`
+ the `hindsight_api.engine.streaming` event protocol) that Hindsight's real
`reflect`/`recall` MCP tools use. `server.py` emits a *scripted* trace so the demo runs
with zero infrastructure (no database, no models).

## How the streaming reaches a client

Every `ProgressEvent` is pushed to **both** MCP notification channels, so the widest
range of clients render something:

| Channel | MCP method | Carries | Client requirement |
|---|---|---|---|
| Progress | `notifications/progress` | `(progress, total, message)` — the human-readable step | a `progressToken` (auto-sent when a `progress_handler` is set) |
| Logging | `notifications/message` | the **full structured event** in `extra["hindsight_event"]` | the client set a logging level (`logging/setLevel`) |

> The structured event is nested under the single key `hindsight_event` because
> FastMCP forwards `extra` into a Python `LogRecord`, which rejects reserved
> attribute names — and the event has a top-level `message` field.

## Run it

In-process (zero infra) — also a self-checking smoke:

```bash
uv run python examples/mcp_streaming/client.py
```

Over HTTP, for an external MCP client:

```bash
# terminal 1 — serve on http://127.0.0.1:8123/mcp/
uv run python examples/mcp_streaming/server.py

# terminal 2 — the example client
uv run python examples/mcp_streaming/client.py http://127.0.0.1:8123/mcp/

# ...or any MCP client tool, e.g. mcporter:
npx mcporter call http://127.0.0.1:8123/mcp/ reflect_demo --query "what have I read?"
```

Point `client.py` at a **real** Hindsight MCP server URL and call the real `reflect`
/ `recall` tools to see live streams of actual reflect loops and recall DAGs.
