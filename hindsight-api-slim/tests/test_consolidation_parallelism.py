"""Tests for the bounded-parallel consolidation path.

Covers:
- ``consolidation_llm_max_concurrent`` is honored (waves see N in-flight calls).
- Exception in one batch does not strand successful batches' consolidated_at
  markers (gather uses ``return_exceptions=True``; succeeded IDs commit before
  the exception is re-raised).
"""

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from hindsight_api.config import _get_raw_config
from hindsight_api.engine.consolidation.consolidator import run_consolidation_job
from hindsight_api.engine.memory_engine import MemoryEngine


@pytest.fixture
def consolidation_concurrency():
    """Pin observations + consolidation_llm_max_concurrent for deterministic concurrency assertions."""
    config = _get_raw_config()
    original_obs = config.enable_observations
    original_concurrency = config.consolidation_llm_max_concurrent
    config.enable_observations = True
    config.consolidation_llm_max_concurrent = 4
    try:
        yield 4
    finally:
        config.enable_observations = original_obs
        config.consolidation_llm_max_concurrent = original_concurrency


async def _ingest_distinct_tag_groups(memory, request_context, bank_id, count):
    """Retain ``count`` memories with distinct single-tag groups so each becomes its own llm_batch."""
    await memory.get_bank_profile(bank_id=bank_id, request_context=request_context)
    for i in range(count):
        await memory.retain_async(
            bank_id=bank_id,
            content=f"Person{i} prefers activity{i}.",
            tags=[f"person{i}"],
            request_context=request_context,
        )


class TestConsolidationParallelism:
    """Bounded-parallel execution of llm_batches within one consolidation op."""

    @pytest.mark.asyncio
    async def test_consolidation_honors_max_concurrent(
        self, memory: MemoryEngine, request_context, consolidation_concurrency
    ):
        """A wave of N llm_batches yields ``N <= max_concurrent`` simultaneously
        in-flight LLM calls — proving the semaphore + gather pair are wired."""
        bank_id = f"test-parallelism-{uuid.uuid4().hex[:8]}"
        n_memories = 8  # > max_concurrent (4) so the cap is exercised

        await _ingest_distinct_tag_groups(memory, request_context, bank_id, n_memories)

        in_flight = 0
        max_in_flight = 0
        gate = asyncio.Lock()

        async def slow_mock_process(*args, **kwargs):
            nonlocal in_flight, max_in_flight
            async with gate:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            try:
                # Hold the slot long enough for siblings to enter
                await asyncio.sleep(0.05)
            finally:
                async with gate:
                    in_flight -= 1
            mems = kwargs.get("memories") or args[4]
            return ([{"action": "skipped"} for _ in mems], 0, False)

        with patch(
            "hindsight_api.engine.consolidation.consolidator._process_memory_batch",
            new=AsyncMock(side_effect=slow_mock_process),
        ):
            await run_consolidation_job(memory, bank_id, request_context)

        assert max_in_flight > 1, f"expected concurrent batches, saw max_in_flight={max_in_flight}"
        assert max_in_flight <= consolidation_concurrency, (
            f"max_in_flight={max_in_flight} exceeded cap {consolidation_concurrency}"
        )

    @pytest.mark.asyncio
    async def test_partial_failure_preserves_succeeded_markers(
        self, memory: MemoryEngine, request_context, consolidation_concurrency
    ):
        """When one batch raises, sibling batches that succeeded must still get
        consolidated_at = NOW(); the exception is re-raised after DB consistency."""
        bank_id = f"test-parallelism-fail-{uuid.uuid4().hex[:8]}"
        n_memories = 6

        await _ingest_distinct_tag_groups(memory, request_context, bank_id, n_memories)

        # Make the second LLM call raise; siblings succeed.
        call_index = 0
        call_index_lock = asyncio.Lock()

        async def mock_process(*args, **kwargs):
            nonlocal call_index
            async with call_index_lock:
                call_index += 1
                idx = call_index
            if idx == 2:
                raise RuntimeError("synthetic LLM failure for batch 2")
            mems = kwargs.get("memories") or args[4]
            return ([{"action": "created"} for _ in mems], 0, False)

        with patch(
            "hindsight_api.engine.consolidation.consolidator._process_memory_batch",
            new=AsyncMock(side_effect=mock_process),
        ), pytest.raises(RuntimeError, match="synthetic LLM failure"):
            await run_consolidation_job(memory, bank_id, request_context)

        # Successful batches' memories must have consolidated_at set; the failed
        # batch's memory must not (will be retried on next consolidation run).
        async with memory._backend.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, consolidated_at FROM memory_units "
                "WHERE bank_id = $1 AND fact_type IN ('experience', 'world')",
                bank_id,
            )
        consolidated = sum(1 for r in rows if r["consolidated_at"] is not None)
        unconsolidated = sum(1 for r in rows if r["consolidated_at"] is None)
        assert consolidated == n_memories - 1, (
            f"expected {n_memories - 1} consolidated (sibling success), got {consolidated}"
        )
        assert unconsolidated == 1, (
            f"expected 1 unconsolidated (failed batch), got {unconsolidated}"
        )
