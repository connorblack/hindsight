"""Tests for consolidation retry budget configurability (issue #1042)."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from hindsight_api.engine.consolidation.consolidator import _consolidate_batch_with_llm, _extract_json_object


@pytest.fixture
def mock_llm_config():
    llm = AsyncMock()
    llm._provider_impl = None
    llm.call.return_value = json.dumps({"creates": [], "updates": [], "deletes": []})
    return llm


@pytest.fixture
def mock_config():
    config = MagicMock()
    config.observations_mission = None
    config.consolidation_max_attempts = 3
    config.consolidation_llm_max_retries = None
    config.consolidation_max_completion_tokens = None
    return config


class TestExtractJsonObject:
    def test_clean_json(self):
        assert _extract_json_object('{"creates": [], "updates": [], "deletes": []}') == {
            "creates": [],
            "updates": [],
            "deletes": [],
        }

    def test_json_fenced(self):
        assert _extract_json_object('```json\n{"creates": [], "updates": [], "deletes": []}\n```') == {
            "creates": [],
            "updates": [],
            "deletes": [],
        }

    def test_leading_prose_then_json(self):
        assert _extract_json_object('Here is the consolidation decision:\n{"creates": [], "updates": [], "deletes": []}') == {
            "creates": [],
            "updates": [],
            "deletes": [],
        }

    def test_trailing_prose_after_json(self):
        assert _extract_json_object('{"creates": [], "updates": [], "deletes": []}\nThat is the final answer.') == {
            "creates": [],
            "updates": [],
            "deletes": [],
        }

    def test_braces_inside_string_values(self):
        payload = _extract_json_object(
            '{"creates": [{"text": "User keeps notes like {draft} in files.", '
            '"source_fact_ids": ["fact-1"], "reason": "The literal {draft} marker is part of the text."}], '
            '"updates": [], "deletes": []}'
        )

        assert payload["creates"][0]["text"] == "User keeps notes like {draft} in files."
        assert payload["creates"][0]["reason"] == "The literal {draft} marker is part of the text."


class TestConsolidationRetryBudget:
    @pytest.mark.asyncio
    async def test_config_is_required(self, mock_llm_config):
        """Passing config=None raises — it's a programmer error, not a runtime fallback."""
        with pytest.raises(ValueError, match="config is required"):
            await _consolidate_batch_with_llm(
                llm_config=mock_llm_config,
                memories=[{"id": "m1", "text": "test"}],
                union_observations=[],
                union_source_facts={},
                config=None,
            )

    @pytest.mark.asyncio
    async def test_configurable_max_attempts(self, mock_llm_config, mock_config):
        """consolidation_max_attempts controls the outer retry loop."""
        mock_config.consolidation_max_attempts = 5
        mock_llm_config.call.side_effect = RuntimeError("fail")
        result = await _consolidate_batch_with_llm(
            llm_config=mock_llm_config,
            memories=[{"id": "m1", "text": "test"}],
            union_observations=[],
            union_source_facts={},
            config=mock_config,
        )
        assert result.failed
        assert mock_llm_config.call.call_count == 5

    @pytest.mark.asyncio
    async def test_max_retries_threaded_to_call(self, mock_llm_config, mock_config):
        """consolidation_llm_max_retries is passed to llm_config.call()."""
        mock_config.consolidation_llm_max_retries = 3
        await _consolidate_batch_with_llm(
            llm_config=mock_llm_config,
            memories=[{"id": "m1", "text": "test"}],
            union_observations=[],
            union_source_facts={},
            config=mock_config,
        )
        assert mock_llm_config.call.call_args.kwargs.get("max_retries") == 3

    @pytest.mark.asyncio
    async def test_response_format_not_sent_for_consolidation_decision(self, mock_llm_config, mock_config):
        """The decide call is free text: no response_format means no server-side JSON grammar."""
        await _consolidate_batch_with_llm(
            llm_config=mock_llm_config,
            memories=[{"id": "m1", "text": "test"}],
            union_observations=[],
            union_source_facts={},
            config=mock_config,
        )
        assert "response_format" not in mock_llm_config.call.call_args.kwargs

    @pytest.mark.asyncio
    async def test_raw_response_is_parsed_and_validated_leniently(self, mock_llm_config, mock_config):
        """A fenced/prose-wrapped raw text response validates into the consolidation model."""
        mock_llm_config.call.return_value = (
            "The decision is:\n"
            "```json\n"
            '{"creates": [{"text": "User likes tea.", "source_fact_ids": ["m1"], "reason": "No match."}], '
            '"updates": [], "deletes": []}\n'
            "```\n"
            "No further changes."
        )
        result = await _consolidate_batch_with_llm(
            llm_config=mock_llm_config,
            memories=[{"id": "m1", "text": "User likes tea."}],
            union_observations=[],
            union_source_facts={},
            config=mock_config,
        )
        assert not result.failed
        assert len(result.creates) == 1
        assert result.creates[0].text == "User likes tea."

    @pytest.mark.asyncio
    async def test_parse_failure_uses_outer_attempt_retry(self, mock_llm_config, mock_config):
        """A malformed free-text response raises inside the attempt and the next attempt can succeed."""
        mock_llm_config.call.side_effect = [
            "I cannot decide yet.",
            '{"creates": [], "updates": [], "deletes": []}',
        ]
        result = await _consolidate_batch_with_llm(
            llm_config=mock_llm_config,
            memories=[{"id": "m1", "text": "test"}],
            union_observations=[],
            union_source_facts={},
            config=mock_config,
        )
        assert not result.failed
        assert mock_llm_config.call.call_count == 2

    @pytest.mark.asyncio
    async def test_user_prompt_contains_exact_schema_and_create_cap(self, mock_llm_config, mock_config):
        """Without server grammar, the prompt carries the exact response contract."""
        await _consolidate_batch_with_llm(
            llm_config=mock_llm_config,
            memories=[{"id": "m1", "text": "test"}],
            union_observations=[],
            union_source_facts={},
            config=mock_config,
            remaining_observation_slots=2,
            max_observations_per_scope=5,
        )
        messages = mock_llm_config.call.call_args.kwargs["messages"]
        user_message = next(m["content"] for m in messages if m["role"] == "user")

        assert "Respond with ONLY a JSON object of this exact shape" in user_message
        assert '"creates": [' in user_message
        assert '"updates": [' in user_message
        assert '"deletes": [' in user_message
        assert '"text": "observation prose"' in user_message
        assert '"source_fact_ids": ["source-fact-uuid"]' in user_message
        assert '"observation_id": "existing-observation-uuid"' in user_message
        assert '"reason": "one sentence explaining the decision"' in user_message
        assert "The creates array must contain no more than 2 item(s)." in user_message

    @pytest.mark.asyncio
    async def test_max_completion_tokens_threaded_to_call(self, mock_llm_config, mock_config):
        """consolidation_max_completion_tokens is passed to llm_config.call()."""
        mock_config.consolidation_max_completion_tokens = 8192
        await _consolidate_batch_with_llm(
            llm_config=mock_llm_config,
            memories=[{"id": "m1", "text": "test"}],
            union_observations=[],
            union_source_facts={},
            config=mock_config,
        )
        assert mock_llm_config.call.call_args.kwargs.get("max_completion_tokens") == 8192

    @pytest.mark.asyncio
    async def test_max_completion_tokens_not_passed_when_none(self, mock_llm_config, mock_config):
        """When consolidation_max_completion_tokens is None, max_completion_tokens is omitted (no regression)."""
        mock_config.consolidation_max_completion_tokens = None
        await _consolidate_batch_with_llm(
            llm_config=mock_llm_config,
            memories=[{"id": "m1", "text": "test"}],
            union_observations=[],
            union_source_facts={},
            config=mock_config,
        )
        assert "max_completion_tokens" not in mock_llm_config.call.call_args.kwargs

    @pytest.mark.asyncio
    async def test_max_retries_not_passed_when_none(self, mock_llm_config, mock_config):
        """When consolidation_llm_max_retries is None, max_retries is not passed."""
        mock_config.consolidation_llm_max_retries = None
        await _consolidate_batch_with_llm(
            llm_config=mock_llm_config,
            memories=[{"id": "m1", "text": "test"}],
            union_observations=[],
            union_source_facts={},
            config=mock_config,
        )
        assert "max_retries" not in mock_llm_config.call.call_args.kwargs

    @pytest.mark.asyncio
    async def test_reduced_budget_limits_total_calls(self, mock_llm_config, mock_config):
        """Setting both to low values caps total failure attempts."""
        mock_config.consolidation_max_attempts = 2
        mock_config.consolidation_llm_max_retries = 2
        mock_llm_config.call.side_effect = RuntimeError("upstream 503")
        result = await _consolidate_batch_with_llm(
            llm_config=mock_llm_config,
            memories=[{"id": "m1", "text": "test"}],
            union_observations=[],
            union_source_facts={},
            config=mock_config,
        )
        assert result.failed
        assert mock_llm_config.call.call_count == 2
        for call_args in mock_llm_config.call.call_args_list:
            assert call_args.kwargs.get("max_retries") == 2
