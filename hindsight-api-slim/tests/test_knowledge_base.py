"""Tests for Knowledge Base (KB) CRUD and KB-MM relationships."""

import uuid

import pytest

from hindsight_api.engine.memory_engine import MemoryEngine


def _unique_bank() -> str:
    return f"test-kb-{uuid.uuid4().hex[:8]}"


class TestKnowledgeBaseCRUD:
    """Basic CRUD operations on knowledge bases."""

    async def test_create_kb(self, memory: MemoryEngine, request_context):
        bank_id = _unique_bank()
        await memory.get_bank_profile(bank_id, request_context=request_context)

        kb = await memory.create_knowledge_base(
            bank_id,
            "my-kb",
            name="My KB",
            mission="Organize test knowledge",
            tags=["test"],
            auto_create=True,
            split_threshold=25,
            request_context=request_context,
        )

        assert kb["id"] == "my-kb"
        assert kb["bank_id"] == bank_id
        assert kb["name"] == "My KB"
        assert kb["mission"] == "Organize test knowledge"
        assert kb["tags"] == ["test"]
        assert kb["auto_create"] is True
        assert kb["split_threshold"] == 25
        assert kb["created_at"] is not None

        await memory.delete_bank(bank_id, request_context=request_context)

    async def test_create_kb_defaults(self, memory: MemoryEngine, request_context):
        bank_id = _unique_bank()
        await memory.get_bank_profile(bank_id, request_context=request_context)

        kb = await memory.create_knowledge_base(
            bank_id,
            "defaults-kb",
            request_context=request_context,
        )

        assert kb["id"] == "defaults-kb"
        assert kb["name"] == "defaults-kb"  # defaults to id
        assert kb["mission"] == ""
        assert kb["tags"] == []
        assert kb["auto_create"] is True
        assert kb["split_threshold"] == 30

        await memory.delete_bank(bank_id, request_context=request_context)

    async def test_list_kbs(self, memory: MemoryEngine, request_context):
        bank_id = _unique_bank()
        await memory.get_bank_profile(bank_id, request_context=request_context)

        await memory.create_knowledge_base(bank_id, "kb-a", name="A", request_context=request_context)
        await memory.create_knowledge_base(bank_id, "kb-b", name="B", request_context=request_context)

        kbs = await memory.list_knowledge_bases(bank_id, request_context=request_context)
        ids = [kb["id"] for kb in kbs]

        assert "kb-a" in ids
        assert "kb-b" in ids
        assert len(kbs) >= 2

        # Each item should have mental_model_count from the JOIN
        for kb in kbs:
            assert "mental_model_count" in kb

        await memory.delete_bank(bank_id, request_context=request_context)

    async def test_get_kb_with_mental_models(self, memory: MemoryEngine, request_context):
        bank_id = _unique_bank()
        await memory.get_bank_profile(bank_id, request_context=request_context)

        await memory.create_knowledge_base(bank_id, "my-kb", name="Test KB", request_context=request_context)

        # Create MMs in this KB
        await memory.create_mental_model(
            bank_id,
            "Preferences",
            "What are the user's preferences?",
            "Test content",
            mental_model_id="prefs",
            kb_id="my-kb",
            request_context=request_context,
        )
        await memory.create_mental_model(
            bank_id,
            "Sources",
            "What sources does the user use?",
            "Test content 2",
            mental_model_id="sources",
            kb_id="my-kb",
            request_context=request_context,
        )

        kb = await memory.get_knowledge_base(bank_id, "my-kb", request_context=request_context)
        assert kb is not None
        assert "mental_models" in kb
        mm_ids = [mm["id"] for mm in kb["mental_models"]]
        assert "prefs" in mm_ids
        assert "sources" in mm_ids

        await memory.delete_bank(bank_id, request_context=request_context)

    async def test_get_kb_not_found(self, memory: MemoryEngine, request_context):
        bank_id = _unique_bank()
        await memory.get_bank_profile(bank_id, request_context=request_context)

        result = await memory.get_knowledge_base(bank_id, "nonexistent", request_context=request_context)
        assert result is None

        await memory.delete_bank(bank_id, request_context=request_context)

    async def test_update_kb(self, memory: MemoryEngine, request_context):
        bank_id = _unique_bank()
        await memory.get_bank_profile(bank_id, request_context=request_context)

        await memory.create_knowledge_base(
            bank_id, "my-kb", name="Original", mission="v1", request_context=request_context
        )

        updated = await memory.update_knowledge_base(
            bank_id,
            "my-kb",
            name="Updated",
            mission="v2",
            tags=["new-tag"],
            auto_create=False,
            split_threshold=50,
            request_context=request_context,
        )

        assert updated is not None
        assert updated["name"] == "Updated"
        assert updated["mission"] == "v2"
        assert updated["tags"] == ["new-tag"]
        assert updated["auto_create"] is False
        assert updated["split_threshold"] == 50

        await memory.delete_bank(bank_id, request_context=request_context)

    async def test_update_kb_partial(self, memory: MemoryEngine, request_context):
        bank_id = _unique_bank()
        await memory.get_bank_profile(bank_id, request_context=request_context)

        await memory.create_knowledge_base(
            bank_id, "my-kb", name="Original", mission="keep this", request_context=request_context
        )

        updated = await memory.update_knowledge_base(
            bank_id,
            "my-kb",
            name="Changed",
            request_context=request_context,
        )

        assert updated["name"] == "Changed"
        assert updated["mission"] == "keep this"  # unchanged

        await memory.delete_bank(bank_id, request_context=request_context)

    async def test_delete_kb_orphan_mms(self, memory: MemoryEngine, request_context):
        bank_id = _unique_bank()
        await memory.get_bank_profile(bank_id, request_context=request_context)

        await memory.create_knowledge_base(bank_id, "my-kb", request_context=request_context)
        await memory.create_mental_model(
            bank_id, "Test", "q", "c", mental_model_id="mm1", kb_id="my-kb", request_context=request_context
        )

        # Delete KB without deleting MMs
        deleted = await memory.delete_knowledge_base(
            bank_id, "my-kb", delete_mental_models=False, request_context=request_context
        )
        assert deleted is True

        # KB gone
        assert await memory.get_knowledge_base(bank_id, "my-kb", request_context=request_context) is None

        # MM still exists but orphaned (kb_id = NULL)
        mms = await memory.list_mental_models(bank_id, request_context=request_context)
        mm_ids = [mm["id"] for mm in mms]
        assert "mm1" in mm_ids

        await memory.delete_bank(bank_id, request_context=request_context)

    async def test_delete_kb_cascade_mms(self, memory: MemoryEngine, request_context):
        bank_id = _unique_bank()
        await memory.get_bank_profile(bank_id, request_context=request_context)

        await memory.create_knowledge_base(bank_id, "my-kb", request_context=request_context)
        await memory.create_mental_model(
            bank_id, "Test", "q", "c", mental_model_id="mm1", kb_id="my-kb", request_context=request_context
        )

        # Delete KB AND its MMs
        deleted = await memory.delete_knowledge_base(
            bank_id, "my-kb", delete_mental_models=True, request_context=request_context
        )
        assert deleted is True

        # Both gone
        mms = await memory.list_mental_models(bank_id, request_context=request_context)
        mm_ids = [mm["id"] for mm in mms]
        assert "mm1" not in mm_ids

        await memory.delete_bank(bank_id, request_context=request_context)


class TestKBMentalModelRelationship:
    """Test the relationship between KBs and mental models."""

    async def test_list_mms_filtered_by_kb(self, memory: MemoryEngine, request_context):
        bank_id = _unique_bank()
        await memory.get_bank_profile(bank_id, request_context=request_context)

        await memory.create_knowledge_base(bank_id, "kb-a", request_context=request_context)
        await memory.create_knowledge_base(bank_id, "kb-b", request_context=request_context)

        await memory.create_mental_model(
            bank_id, "In A", "q", "c", mental_model_id="mm-a", kb_id="kb-a", request_context=request_context
        )
        await memory.create_mental_model(
            bank_id, "In B", "q", "c", mental_model_id="mm-b", kb_id="kb-b", request_context=request_context
        )
        await memory.create_mental_model(
            bank_id, "Standalone", "q", "c", mental_model_id="mm-none", request_context=request_context
        )

        # Filter by kb-a
        mms_a = await memory.list_mental_models(bank_id, kb_id="kb-a", request_context=request_context)
        assert len(mms_a) == 1
        assert mms_a[0]["id"] == "mm-a"

        # Filter by kb-b
        mms_b = await memory.list_mental_models(bank_id, kb_id="kb-b", request_context=request_context)
        assert len(mms_b) == 1
        assert mms_b[0]["id"] == "mm-b"

        # No filter — all 3
        mms_all = await memory.list_mental_models(bank_id, request_context=request_context)
        mm_ids = [mm["id"] for mm in mms_all]
        assert "mm-a" in mm_ids
        assert "mm-b" in mm_ids
        assert "mm-none" in mm_ids

        await memory.delete_bank(bank_id, request_context=request_context)

    async def test_mm_includes_kb_id_in_response(self, memory: MemoryEngine, request_context):
        bank_id = _unique_bank()
        await memory.get_bank_profile(bank_id, request_context=request_context)

        await memory.create_knowledge_base(bank_id, "my-kb", request_context=request_context)
        await memory.create_mental_model(
            bank_id, "Test", "q", "c", mental_model_id="mm1", kb_id="my-kb", request_context=request_context
        )

        mms = await memory.list_mental_models(bank_id, request_context=request_context)
        mm = next(m for m in mms if m["id"] == "mm1")
        assert mm.get("kb_id") == "my-kb"

        await memory.delete_bank(bank_id, request_context=request_context)

    async def test_standalone_mm_has_null_kb_id(self, memory: MemoryEngine, request_context):
        bank_id = _unique_bank()
        await memory.get_bank_profile(bank_id, request_context=request_context)

        await memory.create_mental_model(
            bank_id, "Standalone", "q", "c", mental_model_id="mm-standalone", request_context=request_context
        )

        mms = await memory.list_mental_models(bank_id, request_context=request_context)
        mm = next(m for m in mms if m["id"] == "mm-standalone")
        assert mm.get("kb_id") is None

        await memory.delete_bank(bank_id, request_context=request_context)

    async def test_bank_deletion_cascades_to_kbs(self, memory: MemoryEngine, request_context):
        bank_id = _unique_bank()
        await memory.get_bank_profile(bank_id, request_context=request_context)

        await memory.create_knowledge_base(bank_id, "my-kb", request_context=request_context)

        # Delete the bank — KB should cascade
        await memory.delete_bank(bank_id, request_context=request_context)

        # Verify KB is gone (recreate bank to query)
        await memory.get_bank_profile(bank_id, request_context=request_context)
        kbs = await memory.list_knowledge_bases(bank_id, request_context=request_context)
        assert len(kbs) == 0

        await memory.delete_bank(bank_id, request_context=request_context)
