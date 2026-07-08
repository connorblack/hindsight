"""Rolling retrieval window for mental-model refresh (trigger.rolling_months).

Covers the fix for the frozen-window class: a literal 12-month tag_groups list
baked at template time stops matching the newest month as time advances. With
``rolling_months`` the window is resolved at refresh time.
"""

from datetime import UTC, datetime

import pytest

from hindsight_api.engine.memory_engine import (
    _resolve_refresh_tag_filtering,
    _rolling_month_tags,
)


class TestRollingMonthTags:
    def test_trailing_window_includes_current_month(self):
        tags = _rolling_month_tags(12, today=datetime(2026, 7, 8, tzinfo=UTC))
        assert tags[0] == "month:2025-08"
        assert tags[-1] == "month:2026-07"
        assert len(tags) == 12

    def test_year_boundary(self):
        tags = _rolling_month_tags(3, today=datetime(2026, 1, 15, tzinfo=UTC))
        assert tags == ["month:2025-11", "month:2025-12", "month:2026-01"]

    def test_single_month(self):
        assert _rolling_month_tags(1, today=datetime(2026, 2, 28, tzinfo=UTC)) == ["month:2026-02"]

    def test_ascending_and_unique(self):
        tags = _rolling_month_tags(24, today=datetime(2026, 7, 1, tzinfo=UTC))
        assert len(tags) == len(set(tags)) == 24
        assert tags == sorted(tags)


class TestResolveRollingWindow:
    def test_rolling_months_builds_or_group(self):
        resolved = _resolve_refresh_tag_filtering(["ignored:tag"], {"rolling_months": 12})
        assert resolved.tags is None
        assert resolved.tags_match == "any"
        assert resolved.tag_groups is not None and len(resolved.tag_groups) == 1
        group = resolved.tag_groups[0]
        branches = group.or_ if hasattr(group, "or_") else group.model_dump(by_alias=True)["or"]
        assert len(branches) == 12
        # newest branch is the current month at resolution time
        now = datetime.now(UTC)
        newest = f"month:{now.year:04d}-{now.month:02d}"
        dumped = group.model_dump(by_alias=True)
        all_tags = [b["tags"][0] for b in dumped["or"]]
        assert newest in all_tags

    def test_rolling_months_takes_precedence_over_tag_groups(self):
        # Defense in depth: the API validator rejects both, but raw DB JSONB has
        # no schema guarantee — the resolver must prefer the rolling window.
        resolved = _resolve_refresh_tag_filtering(
            None,
            {
                "rolling_months": 2,
                "tag_groups": [{"or": [{"tags": ["month:2020-01"], "match": "any_strict"}]}],
            },
        )
        dumped = resolved.tag_groups[0].model_dump(by_alias=True)
        assert len(dumped["or"]) == 2
        assert all(b["tags"][0] != "month:2020-01" for b in dumped["or"])

    def test_absent_rolling_months_preserves_existing_behavior(self):
        resolved = _resolve_refresh_tag_filtering(["a"], {})
        assert resolved.tags == ["a"]
        assert resolved.tags_match == "all_strict"
        assert resolved.tag_groups is None


class TestTriggerSchema:
    def test_rolling_months_accepted(self):
        from hindsight_api.api.http import MentalModelTrigger

        t = MentalModelTrigger(rolling_months=12)
        assert t.rolling_months == 12

    def test_rolling_months_and_tag_groups_mutually_exclusive(self):
        from hindsight_api.api.http import MentalModelTrigger

        with pytest.raises(ValueError, match="mutually exclusive"):
            MentalModelTrigger(
                rolling_months=12,
                tag_groups=[{"or": [{"tags": ["month:2026-01"], "match": "any_strict"}]}],
            )

    def test_bounds(self):
        from hindsight_api.api.http import MentalModelTrigger

        with pytest.raises(ValueError):
            MentalModelTrigger(rolling_months=0)
        with pytest.raises(ValueError):
            MentalModelTrigger(rolling_months=121)
