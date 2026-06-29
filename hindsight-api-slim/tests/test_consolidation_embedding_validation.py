import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from hindsight_api import config as config_module
from hindsight_api.engine.consolidation import consolidator


class _ZeroLengthEmbeddings:
    dimension = 384

    def encode_documents(self, texts):
        assert texts == ["Consolidated observation text."]
        return [[]]


class _FakeMemoryEngine:
    embeddings = _ZeroLengthEmbeddings()
    _backend = SimpleNamespace(ops=SimpleNamespace(uses_observation_sources_table=False))

    async def _check_op_alive(self, operation_id):
        return True

    async def _write_operation_progress(self, *args, **kwargs):
        return None


class _FakeMemoryEngineWithObservationSources(_FakeMemoryEngine):
    _backend = SimpleNamespace(ops=SimpleNamespace(uses_observation_sources_table=True))


class _FailingConn:
    async def fetchrow(self, *args, **kwargs):
        raise AssertionError("zero-length embedding should be rejected before database insert")


class _RecordingTransaction:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        assert not self.conn.in_transaction
        self.conn.in_transaction = True
        self.conn.events.append("begin")

    async def __aexit__(self, exc_type, exc, tb):
        self.conn.events.append("rollback" if exc_type else "commit")
        self.conn.in_transaction = False
        return False


class _TransactionalUpdateConn:
    def __init__(self):
        self.events = []
        self.in_transaction = False

    def transaction(self):
        return _RecordingTransaction(self)

    async def execute(self, query, *args, **kwargs):
        if "UPDATE" in query and "memory_units" in query:
            self.events.append(("update", self.in_transaction))
            return "UPDATE 1"
        if "INSERT INTO" in query and "observation_history" in query:
            self.events.append(("history", self.in_transaction))
            return "INSERT 0 1"
        if "DELETE FROM" in query and "observation_sources" in query:
            self.events.append(("source_delete", self.in_transaction))
            return "DELETE 0"
        return "OK 0"

    async def executemany(self, query, args):
        if "observation_sources" in query:
            self.events.append(("source_insert", self.in_transaction, args))
            return "INSERT 0 2"
        raise AssertionError(f"unexpected executemany query: {query}")


@pytest.mark.asyncio
async def test_create_observation_rejects_zero_length_embedding_before_insert(monkeypatch):
    source_id = uuid.uuid4()

    async def fake_filter_live_source_memories(conn, bank_id, source_memory_ids):
        return source_memory_ids

    monkeypatch.setattr(consolidator, "_filter_live_source_memories", fake_filter_live_source_memories)

    with pytest.raises(RuntimeError, match="embedding 0 has dimension 0; expected 384"):
        await consolidator._create_observation_directly(
            conn=_FailingConn(),
            memory_engine=_FakeMemoryEngine(),
            bank_id="test-bank",
            source_memory_ids=[source_id],
            observation_text="Consolidated observation text.",
        )


@pytest.mark.asyncio
async def test_update_batch_leaves_source_unconsumed_when_observation_vanished(monkeypatch):
    source_id = uuid.uuid4()
    observation_id = uuid.uuid4()
    observation = SimpleNamespace(
        id=observation_id,
        text="Existing observation.",
        tags=[],
        source_fact_ids=[],
        occurred_start=None,
        occurred_end=None,
        mentioned_at=None,
    )

    async def fake_find_related_observations(**kwargs):
        return SimpleNamespace(results=[observation], source_facts={})

    async def fake_consolidate_batch_with_llm(**kwargs):
        return consolidator._BatchLLMResult(
            updates=[
                consolidator._UpdateAction(
                    text="Updated observation.",
                    observation_id=str(observation_id),
                    source_fact_ids=[str(source_id)],
                )
            ],
        )

    @asynccontextmanager
    async def fake_acquire_with_retry(pool):
        yield object()

    async def fake_execute_update_action(**kwargs):
        return None

    monkeypatch.setattr(consolidator, "_find_related_observations", fake_find_related_observations)
    monkeypatch.setattr(consolidator, "_consolidate_batch_with_llm", fake_consolidate_batch_with_llm)
    monkeypatch.setattr(consolidator, "acquire_with_retry", fake_acquire_with_retry)
    monkeypatch.setattr(consolidator, "_execute_update_action", fake_execute_update_action)

    results, deleted_count, failed = await consolidator._process_memory_batch(
        memory_engine=_FakeMemoryEngine(),
        bank_id="test-bank",
        memories=[{"id": source_id, "text": "Source fact.", "tags": []}],
        llm_config=object(),
        config=SimpleNamespace(
            max_observations_per_scope=-1,
            observation_scope_limits=None,
            consolidation_dedup_threshold=1.0,
        ),
        request_context=object(),
        pool=object(),
    )

    assert results == [{"action": "skipped", "reason": "update_not_applied", "consume": False}]
    assert deleted_count == 0
    assert failed is False


@pytest.mark.asyncio
async def test_update_batch_keeps_consume_false_when_same_source_is_created_and_unconsumed(monkeypatch):
    source_id = uuid.uuid4()
    observation_id = uuid.uuid4()
    observation = SimpleNamespace(
        id=observation_id,
        text="Existing observation.",
        tags=[],
        source_fact_ids=[],
        occurred_start=None,
        occurred_end=None,
        mentioned_at=None,
    )

    async def fake_find_related_observations(**kwargs):
        return SimpleNamespace(results=[observation], source_facts={})

    async def fake_consolidate_batch_with_llm(**kwargs):
        return consolidator._BatchLLMResult(
            creates=[
                consolidator._CreateAction(
                    text="New durable observation.",
                    source_fact_ids=[str(source_id)],
                )
            ],
            updates=[
                consolidator._UpdateAction(
                    text="Updated observation.",
                    observation_id=str(observation_id),
                    source_fact_ids=[str(source_id)],
                )
            ],
        )

    @asynccontextmanager
    async def fake_acquire_with_retry(pool):
        yield object()

    async def fake_execute_update_action(**kwargs):
        return None

    async def fake_execute_create_action(**kwargs):
        return {"action": "created", "observation_id": str(uuid.uuid4()), "tags": []}

    monkeypatch.setattr(consolidator, "_find_related_observations", fake_find_related_observations)
    monkeypatch.setattr(consolidator, "_consolidate_batch_with_llm", fake_consolidate_batch_with_llm)
    monkeypatch.setattr(consolidator, "acquire_with_retry", fake_acquire_with_retry)
    monkeypatch.setattr(consolidator, "_execute_update_action", fake_execute_update_action)
    monkeypatch.setattr(consolidator, "_execute_create_action", fake_execute_create_action)

    results, deleted_count, failed = await consolidator._process_memory_batch(
        memory_engine=_FakeMemoryEngine(),
        bank_id="test-bank",
        memories=[{"id": source_id, "text": "Source fact.", "tags": []}],
        llm_config=object(),
        config=SimpleNamespace(
            max_observations_per_scope=-1,
            observation_scope_limits=None,
            consolidation_dedup_threshold=1.0,
        ),
        request_context=object(),
        pool=object(),
    )

    assert results == [{"action": "created", "consume": False}]
    assert deleted_count == 0
    assert failed is False


class _StickyScopeConn:
    def __init__(self, source_id):
        self.source_id = source_id
        self.fetch_calls = 0
        self.consolidated_ids = []

    async def fetchrow(self, query, *args, **kwargs):
        if "banks" in query:
            return {"bank_id": "test-bank", "name": "Test Bank"}
        raise AssertionError(f"unexpected fetchrow query: {query}")

    async def fetchval(self, *args, **kwargs):
        return 1 if self.fetch_calls == 0 else 0

    async def fetch(self, *args, **kwargs):
        self.fetch_calls += 1
        if self.fetch_calls == 1:
            return [
                {
                    "id": self.source_id,
                    "text": "Source fact.",
                    "fact_type": "experience",
                    "occurred_start": None,
                    "occurred_end": None,
                    "event_date": None,
                    "tags": ["alpha", "beta"],
                    "mentioned_at": None,
                    "observation_scopes": [["alpha"], ["beta"]],
                }
            ]
        return []

    async def executemany(self, query, args):
        if "SET consolidated_at = NOW()" in query:
            self.consolidated_ids.extend(mem_id for (mem_id,) in args)


@pytest.mark.asyncio
async def test_scope_pass_merge_keeps_unconsumed_source_unstamped(monkeypatch):
    source_id = uuid.uuid4()
    conn = _StickyScopeConn(source_id)

    @asynccontextmanager
    async def fake_acquire_with_retry(pool):
        yield conn

    async def fake_process_memory_batch(**kwargs):
        obs_tags = kwargs["obs_tags_override"]
        if obs_tags == ["alpha"]:
            return [{"action": "skipped", "reason": "update_not_applied", "consume": False}], 0, False
        if obs_tags == ["beta"]:
            return [{"action": "created"}], 0, False
        raise AssertionError(f"unexpected scope tags: {obs_tags}")

    async def fake_trigger_mental_model_refreshes(*args, **kwargs):
        return 0

    monkeypatch.setattr(consolidator, "acquire_with_retry", fake_acquire_with_retry)
    monkeypatch.setattr(consolidator, "_process_memory_batch", fake_process_memory_batch)
    monkeypatch.setattr(consolidator, "_trigger_mental_model_refreshes", fake_trigger_mental_model_refreshes)

    result = await consolidator._run_consolidation_job(
        memory_engine=_FakeMemoryEngine(),
        bank_id="test-bank",
        request_context=object(),
        config=SimpleNamespace(
            enable_observations=True,
            consolidation_batch_size=10,
            consolidation_max_memories_per_round=0,
            consolidation_llm_batch_size=1,
            consolidation_llm_parallelism=1,
        ),
        llm_config=object(),
    )

    assert result["status"] == "completed"
    assert result["memories_processed"] == 1
    assert result["observations_created"] == 1
    assert conn.consolidated_ids == []


@pytest.mark.asyncio
async def test_update_action_keeps_update_history_and_source_sync_in_one_transaction(monkeypatch):
    source_id = uuid.uuid4()
    existing_source_id = uuid.uuid4()
    observation_id = uuid.uuid4()
    conn = _TransactionalUpdateConn()
    observation = SimpleNamespace(
        id=observation_id,
        text="Existing observation.",
        tags=[],
        source_fact_ids=[existing_source_id],
        occurred_start=None,
        occurred_end=None,
        mentioned_at=None,
    )

    async def fake_filter_live_source_memories(conn, bank_id, source_memory_ids):
        return source_memory_ids

    async def fake_generate_embeddings_batch(embeddings, texts):
        assert texts == ["Updated observation."]
        return [[0.1, 0.2]]

    monkeypatch.setattr(consolidator, "_filter_live_source_memories", fake_filter_live_source_memories)
    monkeypatch.setattr(consolidator.embedding_utils, "generate_embeddings_batch", fake_generate_embeddings_batch)
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: SimpleNamespace(enable_observation_history=True, observation_history_max_entries=0),
    )

    result = await consolidator._execute_update_action(
        conn=conn,
        memory_engine=_FakeMemoryEngineWithObservationSources(),
        bank_id="test-bank",
        source_memory_ids=[source_id],
        observation_id=str(observation_id),
        new_text="Updated observation.",
        observations=[observation],
    )

    assert result == "[0.1, 0.2]"
    assert conn.events == [
        "begin",
        ("update", True),
        ("history", True),
        ("source_delete", True),
        ("source_insert", True, [(observation_id, existing_source_id), (observation_id, source_id)]),
        "commit",
    ]


class _ZeroRowUpdateConn:
    def __init__(self):
        self.history_inserts = 0
        self.update_statuses = 0
        self.events = []
        self.in_transaction = False

    def transaction(self):
        return _RecordingTransaction(self)

    async def execute(self, query, *args, **kwargs):
        if "UPDATE" in query and "memory_units" in query:
            self.update_statuses += 1
            return "UPDATE 0"
        if "INSERT INTO" in query and "observation_history" in query:
            self.history_inserts += 1
            return "INSERT 0 0"
        return "OK 0"


@pytest.mark.asyncio
async def test_update_action_returns_none_and_skips_history_when_target_vanished(monkeypatch):
    source_id = uuid.uuid4()
    observation_id = uuid.uuid4()
    conn = _ZeroRowUpdateConn()
    observation = SimpleNamespace(
        id=observation_id,
        text="Existing observation.",
        tags=[],
        source_fact_ids=[],
        occurred_start=None,
        occurred_end=None,
        mentioned_at=None,
    )

    async def fake_filter_live_source_memories(conn, bank_id, source_memory_ids):
        return source_memory_ids

    async def fake_generate_embeddings_batch(embeddings, texts):
        assert texts == ["Updated observation."]
        return [[0.1, 0.2]]

    monkeypatch.setattr(consolidator, "_filter_live_source_memories", fake_filter_live_source_memories)
    monkeypatch.setattr(consolidator.embedding_utils, "generate_embeddings_batch", fake_generate_embeddings_batch)
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: SimpleNamespace(enable_observation_history=True, observation_history_max_entries=0),
    )

    result = await consolidator._execute_update_action(
        conn=conn,
        memory_engine=_FakeMemoryEngine(),
        bank_id="test-bank",
        source_memory_ids=[source_id],
        observation_id=str(observation_id),
        new_text="Updated observation.",
        observations=[observation],
    )

    assert result is None
    assert conn.update_statuses == 1
    assert conn.history_inserts == 0
