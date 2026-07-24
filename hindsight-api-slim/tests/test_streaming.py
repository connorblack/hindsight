"""Unit tests for the reflect/recall progress-streaming protocol (engine.streaming)."""

import asyncio

import pytest

from hindsight_api.engine.streaming import (
    ProgressEmitter,
    ProgressEvent,
    ProgressOperation,
    ProgressPhase,
    ProgressStatus,
    QueueProgressEmitter,
    RecallStageData,
    ReflectToolData,
    RetrievalMethodStat,
    format_sse,
)


async def _drain(emitter: QueueProgressEmitter) -> list[ProgressEvent]:
    return [event async for event in emitter.events()]


@pytest.mark.asyncio
async def test_queue_emitter_orders_events_and_stamps_monotonic_seq():
    emitter = QueueProgressEmitter(ProgressOperation.REFLECT, "op-1")
    await emitter.send(
        phase=ProgressPhase.ITERATION,
        status=ProgressStatus.STARTED,
        message="iter 1",
    )
    await emitter.send(
        phase=ProgressPhase.TOOL_CALL,
        status=ProgressStatus.COMPLETED,
        message="recall done",
        data=ReflectToolData(iteration=1, tool="recall", input_summary="query=x", output_chars=42, duration_ms=100),
    )
    await emitter.aclose()

    events = await _drain(emitter)
    assert [e.seq for e in events] == [1, 2]
    assert events[0].operation is ProgressOperation.REFLECT
    assert events[0].operation_id == "op-1"
    assert events[1].data.tool == "recall"
    assert events[1].data.duration_ms == 100


@pytest.mark.asyncio
async def test_events_terminates_only_on_aclose():
    emitter = QueueProgressEmitter(ProgressOperation.RECALL, "op-2")
    await emitter.send(phase=ProgressPhase.RETRIEVAL, status=ProgressStatus.COMPLETED, message="retrieval")

    # events() must block on the queue, not terminate, until aclose() pushes the sentinel.
    drain = asyncio.create_task(_drain(emitter))
    await asyncio.sleep(0)
    assert not drain.done()
    await emitter.aclose()
    events = await drain
    assert len(events) == 1


@pytest.mark.asyncio
async def test_backpressure_drops_instead_of_blocking_the_engine():
    # A tiny queue that we never drain: sends must never raise or block, because
    # progress is advisory and the engine must not stall on a slow consumer.
    emitter = QueueProgressEmitter(ProgressOperation.RECALL, "op-3", maxsize=2)
    for i in range(50):
        await asyncio.wait_for(
            emitter.send(phase=ProgressPhase.FUSION, status=ProgressStatus.COMPLETED, message=f"n{i}"),
            timeout=1.0,
        )


def test_discriminated_union_round_trips_through_json():
    event = ProgressEvent(
        operation=ProgressOperation.RECALL,
        operation_id="op-4",
        seq=1,
        phase=ProgressPhase.RETRIEVAL,
        status=ProgressStatus.COMPLETED,
        ts="2026-07-24T00:00:00Z",
        message="retrieval",
        data=RecallStageData(
            stage="retrieval",
            count=2312,
            duration_s=12.86,
            methods=[RetrievalMethodStat(fact_type="world", method="graph", count=1000, duration_s=2.1)],
        ),
    )
    restored = ProgressEvent.model_validate_json(event.model_dump_json())
    assert isinstance(restored.data, RecallStageData)
    assert restored.data.methods[0].method == "graph"


def test_format_sse_frame_shape():
    event = ProgressEvent(
        operation=ProgressOperation.REFLECT,
        operation_id="op-5",
        seq=1,
        phase=ProgressPhase.FINAL,
        status=ProgressStatus.FINAL,
        ts="2026-07-24T00:00:00Z",
        message="done",
    )
    frame = format_sse("progress", event)
    assert frame.startswith("event: progress\ndata: {")
    assert frame.endswith("}\n\n")
    # Single data line — model_dump_json emits no embedded newlines.
    assert frame.count("\ndata: ") == 1


@pytest.mark.asyncio
async def test_base_emitter_is_a_usable_noop():
    # The base class must be a working no-op sink so engine guards stay simple.
    emitter = ProgressEmitter(ProgressOperation.REFLECT, "op-6")
    await emitter.send(phase=ProgressPhase.ITERATION, status=ProgressStatus.STARTED, message="noop")
