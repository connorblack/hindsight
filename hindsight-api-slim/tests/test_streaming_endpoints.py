"""Integration tests for the SSE streaming reflect/recall endpoints.

Exercises the real FastAPI app (MockLLM + embedded pg0): retains a memory, then
streams recall and reflect, parsing the SSE frames. Also indirectly covers the
_build_recall_response / _build_reflect_response extraction shared with the JSON
endpoints (the terminal `result` frame uses the same builders).
"""

import json
from datetime import datetime

import httpx
import pytest
import pytest_asyncio

from hindsight_api.api import create_app


@pytest_asyncio.fixture
async def api_client(memory):
    app = create_app(memory, initialize_memory=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
def test_bank_id():
    return f"stream_test_{datetime.now().timestamp()}"


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    """Parse an SSE body into (event_name, data_dict) tuples."""
    events: list[tuple[str, dict]] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        name: str | None = None
        data: str | None = None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data = line.removeprefix("data: ")
        if name is not None:
            events.append((name, json.loads(data) if data else {}))
    return events


async def _retain(api_client: httpx.AsyncClient, bank: str) -> None:
    resp = await api_client.post(
        f"/v1/default/banks/{bank}/memories",
        json={"items": [{"content": "Alice is a machine learning researcher at Stanford.", "context": "team"}]},
    )
    assert resp.status_code == 200


async def _collect_stream(api_client: httpx.AsyncClient, url: str, payload: dict) -> list[tuple[str, dict]]:
    async with api_client.stream("POST", url, json=payload) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = ""
        async for chunk in resp.aiter_text():
            body += chunk
    return _parse_sse(body)


@pytest.mark.asyncio
async def test_recall_stream_emits_stage_progress_then_result(api_client, test_bank_id):
    await _retain(api_client, test_bank_id)
    events = await _collect_stream(
        api_client,
        f"/v1/default/banks/{test_bank_id}/memories/recall/stream",
        {"query": "machine learning researcher", "trace": True},
    )
    names = [n for n, _ in events]
    assert "result" in names and "done" in names

    # The recall DAG always runs all three stages -> three stage-progress events.
    stages = [
        (d.get("data") or {}).get("stage")
        for n, d in events
        if n == "progress" and (d.get("data") or {}).get("kind") == "recall_stage"
    ]
    assert {"retrieval", "fusion", "rerank"}.issubset(set(stages))

    # The terminal result frame carries the same shaped RecallResponse the JSON endpoint returns.
    result = next(d for n, d in events if n == "result")
    assert "results" in result

    # Progress frames strictly precede the result frame.
    assert names.index("result") > max(i for i, n in enumerate(names) if n == "progress")


@pytest.mark.asyncio
async def test_reflect_stream_returns_result_frame(api_client, test_bank_id):
    await _retain(api_client, test_bank_id)
    events = await _collect_stream(
        api_client,
        f"/v1/default/banks/{test_bank_id}/reflect/stream",
        {"query": "What do you know about Alice?"},
    )
    names = [n for n, _ in events]
    assert "result" in names and "done" in names
    result = next(d for n, d in events if n == "result")
    assert "text" in result
