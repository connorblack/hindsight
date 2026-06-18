"""Per-operation strict_schema resolution + configurable retain temperature.

Deterministic (no LLM): covers the scope-aware strict_schema resolver and the
``from_env`` parsing of the new per-op override vars.
"""

import pytest

from hindsight_api.config import HindsightConfig
from hindsight_api.engine.llm_wrapper import _strict_schema_for_scope


class _Cfg:
    """Minimal stand-in exposing only the fields the resolver reads."""

    def __init__(
        self,
        *,
        llm_strict_schema: bool = True,
        retain: bool | None = None,
        consolidation: bool | None = None,
        reflect: bool | None = None,
    ) -> None:
        self.llm_strict_schema = llm_strict_schema
        self.retain_llm_strict_schema = retain
        self.consolidation_llm_strict_schema = consolidation
        self.reflect_llm_strict_schema = reflect


def test_unset_overrides_fall_back_to_global():
    cfg = _Cfg(llm_strict_schema=True)
    assert _strict_schema_for_scope("retain_extract_facts", cfg) is True
    assert _strict_schema_for_scope("consolidation", cfg) is True
    assert _strict_schema_for_scope("reflect", cfg) is True
    # Unknown scopes (verification probes, etc.) also inherit the global flag.
    assert _strict_schema_for_scope("verification", cfg) is True


def test_per_op_override_wins_over_global():
    cfg = _Cfg(llm_strict_schema=True, consolidation=False, reflect=False)
    assert _strict_schema_for_scope("retain_extract_facts", cfg) is True  # retain inherits global
    assert _strict_schema_for_scope("consolidation", cfg) is False
    assert _strict_schema_for_scope("consolidation_dedup", cfg) is False  # prefix match
    assert _strict_schema_for_scope("reflect", cfg) is False
    assert _strict_schema_for_scope("verification", cfg) is True  # untouched


def test_override_false_beats_global_true():
    cfg = _Cfg(llm_strict_schema=True, retain=False)
    assert _strict_schema_for_scope("retain_extract_facts", cfg) is False


@pytest.mark.parametrize("raw,expected", [("true", True), ("1", True), ("false", False), ("0", False)])
def test_from_env_parses_strict_overrides(monkeypatch, raw, expected):
    monkeypatch.setenv("HINDSIGHT_API_RETAIN_LLM_STRICT_SCHEMA", raw)
    monkeypatch.setenv("HINDSIGHT_API_CONSOLIDATION_LLM_STRICT_SCHEMA", raw)
    monkeypatch.setenv("HINDSIGHT_API_REFLECT_LLM_STRICT_SCHEMA", raw)
    c = HindsightConfig.from_env()
    assert c.retain_llm_strict_schema is expected
    assert c.consolidation_llm_strict_schema is expected
    assert c.reflect_llm_strict_schema is expected


def test_from_env_strict_unset_is_none(monkeypatch):
    for var in (
        "HINDSIGHT_API_RETAIN_LLM_STRICT_SCHEMA",
        "HINDSIGHT_API_CONSOLIDATION_LLM_STRICT_SCHEMA",
        "HINDSIGHT_API_REFLECT_LLM_STRICT_SCHEMA",
    ):
        monkeypatch.delenv(var, raising=False)
    c = HindsightConfig.from_env()
    assert c.retain_llm_strict_schema is None
    assert c.consolidation_llm_strict_schema is None
    assert c.reflect_llm_strict_schema is None


def test_from_env_retain_temperature(monkeypatch):
    monkeypatch.delenv("HINDSIGHT_API_RETAIN_LLM_TEMPERATURE", raising=False)
    assert HindsightConfig.from_env().retain_llm_temperature == 0.1
    monkeypatch.setenv("HINDSIGHT_API_RETAIN_LLM_TEMPERATURE", "0.2")
    assert HindsightConfig.from_env().retain_llm_temperature == 0.2
