"""Deterministic unit tests for the consolidation duplicate-create guard.

These exercise the dedup decision directly (no LLM, no DB), so they reliably
guard the fix in CI — unlike the real-LLM integration test, which only triggers
the path stochastically.
"""

import types
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from hindsight_api.engine.consolidation.consolidator import (
    _dedup_active,
    _dedup_reconcile_create,
    _dedup_reconcile_update,
    _DedupDecision,
    _duplicate_create_target,
    _duplicate_obs_key,
    _execute_update_action,
    _norm_obs_text,
)
from hindsight_api.engine.search.types import RetrievalResult


@dataclass
class _FakeObs:
    id: str
    text: str
    occurred_start: object | None = None
    occurred_end: object | None = None
    event_date: object | None = None


def _shown(*observations: _FakeObs) -> dict[str, _FakeObs]:
    return {
        _duplicate_obs_key(
            o.text,
            occurred_start=o.occurred_start,
            occurred_end=o.occurred_end,
            event_date=o.event_date,
        ): o
        for o in observations
    }


def test_norm_obs_text_collapses_whitespace_preserves_case() -> None:
    # Whitespace (incl. newlines) collapses; case is preserved.
    assert _norm_obs_text("  The  User  likes BASIL.\n") == "The User likes BASIL."
    assert _norm_obs_text(None) == ""


def test_create_matching_shown_observation_is_duplicate() -> None:
    shown = _shown(_FakeObs(id="11111111-aaaa", text="User waters the herbs early in the morning."))
    # Same text with only-whitespace differences still matches.
    target = _duplicate_create_target("User waters the   herbs early in the morning.", shown, set())
    assert target is not None
    assert target.startswith("shown observation 11111111")


def test_create_differing_only_in_case_is_not_duplicate() -> None:
    # Case-folding would lose information (e.g. acronyms), so a case-only difference
    # is treated as novel rather than silently dropped.
    shown = _shown(_FakeObs(id="22222222-bbbb", text="The user prefers TLS."))
    assert _duplicate_create_target("The user prefers tls.", shown, set()) is None


def test_create_matching_inresponse_update_is_duplicate() -> None:
    update_texts = {_duplicate_obs_key("Mint is kept in its own separate bed.")}
    target = _duplicate_create_target("Mint is kept in its own separate bed.", {}, update_texts)
    assert target == "an UPDATE in this response"


def test_create_same_text_different_time_is_not_duplicate() -> None:
    shown = _shown(
        _FakeObs(
            id="11111111-aaaa",
            text="User watered the herbs.",
            occurred_start="2024-01-15T10:30:00Z",
        )
    )
    target = _duplicate_create_target(
        "User watered the herbs.",
        shown,
        set(),
        occurred_start=datetime(2024, 1, 16, 10, 30, tzinfo=timezone.utc),
    )
    assert target is None


def test_create_same_text_same_time_is_duplicate() -> None:
    shown = _shown(
        _FakeObs(
            id="11111111-aaaa",
            text="User watered the herbs.",
            occurred_start="2024-01-15T10:30:00Z",
        )
    )
    target = _duplicate_create_target(
        "User watered the herbs.",
        shown,
        set(),
        occurred_start=datetime(2024, 1, 15, 10, 30, tzinfo=timezone.utc),
    )
    assert target is not None
    assert target.startswith("shown observation 11111111")


def test_novel_create_is_not_duplicate() -> None:
    shown = _shown(_FakeObs(id="22222222-bbbb", text="User waters the herbs early in the morning."))
    assert _duplicate_create_target("Rosemary is drought-tolerant.", shown, set()) is None
    assert _duplicate_create_target("", {}, set()) is None


# ── semantic dedup (_dedup_reconcile_create) ──────────────────────────────────
#
# Mocks the embedder, the obs-anchored ANN probe, and the LLM so the decision logic is
# tested without a DB or a real model.

_TWIN_ID = "33333333-3333-4333-8333-333333333333"


def _obs(text: str, sim: float, oid: str = _TWIN_ID) -> RetrievalResult:
    return RetrievalResult(id=oid, text=text, fact_type="observation", similarity=sim)


def _ctx(threshold: float = 0.97):
    """Return (kwargs, conn_mock, llm_mock) for a _dedup_reconcile_create call."""
    conn = AsyncMock()
    conn.execute.return_value = "UPDATE 1"
    llm = types.SimpleNamespace(call=AsyncMock())
    kwargs = dict(
        conn=conn,
        memory_engine=types.SimpleNamespace(embeddings=object()),
        bank_id="bank1",
        config=types.SimpleNamespace(consolidation_dedup_threshold=threshold),
        dedup_llm_config=llm,
        create_text="YouTube content in Uzbek is very rich.",
        create_source_ids=[uuid.uuid4()],
        tags=["t1"],
    )
    return kwargs, conn, llm


def _patch_probe(results):
    return patch(
        "hindsight_api.engine.search.retrieval.retrieve_semantic_bm25_combined",
        AsyncMock(return_value={"observation": (results, [])}),
    )


def _patch_embed():
    return patch(
        "hindsight_api.engine.retain.embedding_utils.generate_embeddings_batch",
        AsyncMock(return_value=[[0.1, 0.2, 0.3]]),
    )


async def test_dedup_no_twin_above_threshold_returns_none() -> None:
    kwargs, conn, llm = _ctx(threshold=0.97)
    with _patch_embed(), _patch_probe([_obs("something loosely related", 0.81)]):
        result = await _dedup_reconcile_create(**kwargs)
    assert result is None
    llm.call.assert_not_called()  # below threshold → no LLM call
    conn.execute.assert_not_called()  # no merge


async def test_dedup_llm_keep_does_not_merge() -> None:
    kwargs, conn, llm = _ctx()
    llm.call.return_value = _DedupDecision(action="keep", reason="different language")
    with _patch_embed(), _patch_probe([_obs("Uzbek content on YouTube is described as very rich.", 0.98)]):
        result = await _dedup_reconcile_create(**kwargs)
    assert result is None
    llm.call.assert_awaited_once()
    conn.execute.assert_not_called()  # kept distinct → no merge


async def test_dedup_llm_merge_folds_into_twin() -> None:
    kwargs, conn, llm = _ctx()
    kwargs["create_source_ids"] = [uuid.uuid4(), uuid.uuid4()]
    llm.call.return_value = _DedupDecision(action="merge", text="Uzbek content on YouTube is very rich.")
    with _patch_embed(), _patch_probe([_obs("Uzbek content on YouTube is described as very rich.", 0.99)]):
        result = await _dedup_reconcile_create(**kwargs)
    assert result == _TWIN_ID  # merged into the twin; caller skips the CREATE
    conn.execute.assert_awaited_once()
    args = conn.execute.await_args.args
    assert args[1] == "Uzbek content on YouTube is very rich."  # merged text persisted
    assert args[2] == kwargs["create_source_ids"]  # new source facts folded in
    assert args[3] == uuid.UUID(_TWIN_ID)  # onto the twin row


async def test_dedup_picks_highest_above_threshold_skips_below() -> None:
    # Only the >=threshold candidate is considered; a 0.95 result is ignored at threshold 0.97.
    kwargs, conn, llm = _ctx(threshold=0.97)
    llm.call.return_value = _DedupDecision(action="keep")
    with _patch_embed(), _patch_probe([_obs("near but distinct", 0.95), _obs("the real twin", 0.98)]):
        await _dedup_reconcile_create(**kwargs)
    # the twin passed to the LLM is the >=0.97 one, not the 0.95
    sent = llm.call.await_args.kwargs["messages"][0]["content"]
    assert "the real twin" in sent
    assert "near but distinct" not in sent


# ── UPDATE-path dedup (_dedup_reconcile_update) ───────────────────────────────
#
# An UPDATE rewrites+re-embeds an observation, which can drift it into a near-twin of a
# DIFFERENT existing observation. These cover the fold-and-delete reconciliation (unlike
# CREATE, both rows already exist), the self-exclusion, and the keep/no-twin no-ops.

_UPDATED_ID = "44444444-4444-4444-8444-444444444444"


def _update_ctx(threshold: float = 0.97):
    """Return (kwargs, conn_mock, llm_mock) for a _dedup_reconcile_update call."""
    conn = AsyncMock()
    conn.execute.side_effect = ["UPDATE 1", "DELETE 1"]
    conn.fetch.return_value = [{"id": uuid.UUID(_TWIN_ID)}, {"id": uuid.UUID(_UPDATED_ID)}]
    conn.transaction = MagicMock(return_value=AsyncMock())
    llm = types.SimpleNamespace(call=AsyncMock())
    kwargs = dict(
        conn=conn,
        memory_engine=types.SimpleNamespace(embeddings=object()),
        bank_id="bank1",
        config=types.SimpleNamespace(consolidation_dedup_threshold=threshold),
        dedup_llm_config=llm,
        updated_id=_UPDATED_ID,
        updated_text="Uzbek content on YouTube is very rich and growing.",
        updated_emb_str="[0.1, 0.2, 0.3]",  # already embedded by _execute_update_action
        tags=["t1"],
    )
    return kwargs, conn, llm


async def test_dedup_update_merge_folds_into_twin_and_deletes_updated() -> None:
    kwargs, conn, llm = _update_ctx()
    llm.call.return_value = _DedupDecision(action="merge", text="Uzbek YouTube content is very rich and growing.")
    with _patch_probe([_obs("Uzbek content on YouTube is described as very rich.", 0.98)]):
        await _dedup_reconcile_update(**kwargs)
    llm.call.assert_awaited_once()
    conn.fetch.assert_awaited_once()
    lock_args = conn.fetch.await_args.args
    assert "ORDER BY id" in lock_args[0]
    assert "FOR UPDATE" in lock_args[0]
    assert lock_args[1] == [uuid.UUID(_TWIN_ID), uuid.UUID(_UPDATED_ID)]
    # Two writes: fold-into-twin UPDATE, then DELETE of the updated row.
    assert conn.execute.await_count == 2
    fold_args = conn.execute.await_args_list[0].args
    assert "t.tags || u.tags" in fold_args[0]
    assert fold_args[1] == "Uzbek YouTube content is very rich and growing."  # merged text on the twin
    assert fold_args[2] == uuid.UUID(_TWIN_ID)  # survivor = the twin
    assert fold_args[3] == uuid.UUID(_UPDATED_ID)  # folded-from = the updated row
    delete_args = conn.execute.await_args_list[1].args
    assert delete_args[1] == uuid.UUID(_UPDATED_ID)  # the updated row is deleted


async def test_dedup_update_keep_does_not_merge() -> None:
    kwargs, conn, llm = _update_ctx()
    llm.call.return_value = _DedupDecision(action="keep", reason="different growth claim")
    with _patch_probe([_obs("Uzbek content on YouTube is described as very rich.", 0.98)]):
        await _dedup_reconcile_update(**kwargs)
    llm.call.assert_awaited_once()
    conn.execute.assert_not_called()  # kept distinct → neither fold nor delete


async def test_dedup_update_excludes_self() -> None:
    # The probe surfaces the updated observation itself at 1.0; it must be excluded so we don't
    # "merge" a row into itself. With no other candidate, there is no twin → no LLM, no writes.
    kwargs, conn, llm = _update_ctx()
    with _patch_probe([_obs("its own current text", 1.0, oid=_UPDATED_ID)]):
        await _dedup_reconcile_update(**kwargs)
    llm.call.assert_not_called()
    conn.execute.assert_not_called()


async def test_dedup_update_no_twin_above_threshold() -> None:
    kwargs, conn, llm = _update_ctx(threshold=0.97)
    with _patch_probe([_obs("loosely related", 0.8)]):
        await _dedup_reconcile_update(**kwargs)
    llm.call.assert_not_called()
    conn.execute.assert_not_called()


async def test_execute_update_action_postgres_appends_sources_tags_and_temporal_fields() -> None:
    obs_id = uuid.uuid4()
    source_id = uuid.uuid4()
    occurred_start = datetime(2024, 1, 15, 10, 30, tzinfo=timezone.utc)
    occurred_end = datetime(2024, 1, 15, 11, 30, tzinfo=timezone.utc)
    mentioned_at = datetime(2024, 1, 16, 9, 0, tzinfo=timezone.utc)
    conn = AsyncMock()
    conn.fetch.return_value = [{"id": source_id}]
    conn.fetchrow.return_value = {"id": obs_id}
    memory_engine = types.SimpleNamespace(
        embeddings=object(),
        _backend=types.SimpleNamespace(ops=types.SimpleNamespace(uses_observation_sources_table=False)),
    )
    observations = [
        types.SimpleNamespace(
            id=str(obs_id),
            text="Old observation",
            tags=["existing"],
            source_fact_ids=[str(uuid.uuid4())],
            occurred_start=None,
            occurred_end=None,
            mentioned_at=None,
        )
    ]

    with (
        _patch_embed(),
        patch(
            "hindsight_api.config.get_config",
            return_value=types.SimpleNamespace(enable_observation_history=False),
        ),
    ):
        result = await _execute_update_action(
            conn=conn,
            memory_engine=memory_engine,
            bank_id="bank1",
            source_memory_ids=[source_id],
            observation_id=str(obs_id),
            new_text="Updated observation",
            observations=observations,
            source_fact_tags=["new-tag"],
            source_occurred_start=occurred_start,
            source_occurred_end=occurred_end,
            source_mentioned_at=mentioned_at,
        )

    assert result.status == "updated"
    assert result.embedding_str == "[0.1, 0.2, 0.3]"
    assert result.live_source_memory_ids == [source_id]
    sql = conn.fetchrow.await_args.args[0]
    assert "source_memory_ids = (SELECT array_agg(DISTINCT e)" in sql
    assert "tags              = (SELECT array_agg(DISTINCT e)" in sql
    args = conn.fetchrow.await_args.args
    assert args[1] == "Updated observation"
    assert args[2] == "[0.1, 0.2, 0.3]"
    assert args[3] == [source_id]
    assert args[4] == ["new-tag"]
    assert args[5] == obs_id
    assert args[6] == occurred_start
    assert args[7] == occurred_end
    assert args[8] == mentioned_at


# ── dedup activation gate (_dedup_active) ─────────────────────────────────────
#
# Enabled by default (threshold < 1.0), but skipped on Oracle because the merge path is
# Postgres-only — so the feature can ship on-by-default without breaking Oracle.


def _gate_cfg(threshold: float):
    return types.SimpleNamespace(consolidation_dedup_threshold=threshold)


def _patch_backend(name: str):
    return patch(
        "hindsight_api.engine.consolidation.consolidator.get_config",
        return_value=types.SimpleNamespace(database_backend=name),
    )


def test_dedup_active_enabled_on_postgres() -> None:
    with _patch_backend("postgresql"):
        assert _dedup_active(_gate_cfg(0.97)) is True


def test_dedup_active_disabled_when_threshold_is_one() -> None:
    with _patch_backend("postgresql"):
        assert _dedup_active(_gate_cfg(1.0)) is False


def test_dedup_active_skipped_on_oracle() -> None:
    # PG-only merge path → dedup is skipped on Oracle even with a sub-1.0 threshold.
    with _patch_backend("oracle"):
        assert _dedup_active(_gate_cfg(0.97)) is False


def test_dedup_active_none_config() -> None:
    assert _dedup_active(None) is False
