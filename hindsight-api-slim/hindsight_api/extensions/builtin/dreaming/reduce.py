"""Async "dreaming" dedup-reduce over committed observations.

This is an ADDITIVE Hindsight extension. It reuses the SAME dedup machinery the
core consolidator uses so a "dream" merge talks to the SAME consolidation LLM
with the SAME temporal-conservative prompt:

  * ``_DEDUP_PROMPT``                — verbatim merge/keep prompt (temporal keep rule)
  * ``_DedupDecision``              — merge/keep + synthesized text schema
  * ``_norm_obs_text``             — case-preserving, whitespace-collapsing, temporally lossless
  * ``_append_observation_history`` — pre-overwrite snapshot
  * ``_ObservationHistorySnapshot``
  * ``embedding_utils.generate_embeddings_batch`` — same embedder as retain/consolidate
  * ``fq_table`` / ``acquire_with_retry`` — schema-qualified tables + pooled connections

Governing principle — temporal conservatism (Hindsight != Honcho): this is a
daily-journaling, temporally-resolved corpus. NEVER merge across distinct
dates/times/numbers/negations/entities. Embedding similarity is recall-only;
the LLM is the precision gate. The day/week/month write-scopes already isolate
cross-time states, so the reduce only ever compares observations WITHIN one
scope (``tags @> $scope``).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

# --- Reuse core machinery VERBATIM (imported, never re-implemented) ----------
from hindsight_api.config import get_config
from hindsight_api.engine.consolidation.consolidator import (
    _DEDUP_PROMPT,
    _append_observation_history,
    _DedupDecision,
    _norm_obs_text,
    _ObservationHistorySnapshot,
)
from hindsight_api.engine.db_utils import acquire_with_retry
from hindsight_api.engine.memory_engine import fq_table
from hindsight_api.engine.retain import embedding_utils

from .config import DreamingConfig

if TYPE_CHECKING:  # pragma: no cover - typing only
    from asyncpg import Connection

    from hindsight_api import MemoryEngine
    from hindsight_api.models import RequestContext

logger = logging.getLogger(__name__)

ARCHIVE_TABLE = "dreaming_archived_observations"
ARCHIVE_HISTORY_TABLE = "dreaming_archived_observation_history"

# PostgreSQL SQLSTATEs that mean "retry the transaction": lock_not_available
# (FOR UPDATE NOWAIT lost the race), serialization_failure, deadlock_detected.
_CONFLICT_SQLSTATES = {"55P03", "40001", "40P01"}
_APPLY_RETRIES = 4

# The LLM scope/operation label for trace rows (distinct from core consolidation).
_DREAM_LLM_OP = "dreaming_reduce"


# ---------------------------------------------------------------------------
# Small value objects returned to the HTTP/MCP layer (mapped to Pydantic there)
# ---------------------------------------------------------------------------
@dataclass
class MemberView:
    """One observation in a proposed/applied cluster."""

    id: str
    text: str
    occurred_start: str | None
    occurred_end: str | None
    mentioned_at: str | None
    # "survivor" | "exact-text" | "semantic" | "kept"
    role: str
    reason: str = ""


@dataclass
class ClusterPlan:
    """A proposed reduce of one cluster: the survivor + the members folded in."""

    scope: list[str]
    survivor_id: str
    merged_text: str
    members: list[MemberView]
    merged_member_ids: list[str]  # ids that will be archived + deleted
    applied: bool = False
    archived_count: int = 0


@dataclass
class ScopeResult:
    scope: list[str]
    observations_scanned: int
    clusters_proposed: int
    observations_merged: int
    clusters: list[ClusterPlan] = field(default_factory=list)
    error: str | None = None


@dataclass
class DreamReport:
    mode: str
    bank_id: str
    scopes_scanned: int
    clusters_proposed: int
    observations_merged: int
    scopes: list[ScopeResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _is_conflict(exc: Exception) -> bool:
    return getattr(exc, "sqlstate", None) in _CONFLICT_SQLSTATES


class InvalidDreamScope(ValueError):
    """Raised when a caller supplies a scope that would widen temporal isolation."""


def _quote_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _column_list(columns: list[str], alias: str | None = None) -> str:
    prefix = f"{alias}." if alias else ""
    return ", ".join(f"{prefix}{_quote_ident(col)}" for col in columns)


async def _table_columns(conn: "Connection", table: str) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT a.attname AS name, format_type(a.atttypid, a.atttypmod) AS type_sql
        FROM pg_attribute a
        WHERE a.attrelid = $1::regclass
          AND a.attnum > 0
          AND NOT a.attisdropped
        ORDER BY a.attnum
        """,
        table,
    )
    return [dict(r) for r in rows]


async def _table_column_names(conn: "Connection", table: str) -> list[str]:
    return [r["name"] for r in await _table_columns(conn, table)]


async def _sync_archive_columns(conn: "Connection", source_table: str, archive_table: str) -> list[str]:
    """Ensure the archive table has every current source column and return source order."""

    source_columns = await _table_columns(conn, source_table)
    archive_column_names = set(await _table_column_names(conn, archive_table))
    for col in source_columns:
        name = col["name"]
        if name in archive_column_names:
            continue
        await conn.execute(
            f"ALTER TABLE {archive_table} ADD COLUMN {_quote_ident(name)} {col['type_sql']}"
        )
        archive_column_names.add(name)
    return [col["name"] for col in source_columns]


def _temporal_key(row: dict[str, Any]) -> tuple[Any, Any, Any, Any]:
    return (
        row.get("event_date"),
        row.get("occurred_start"),
        row.get("occurred_end"),
        row.get("mentioned_at"),
    )


def _same_temporal(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return _temporal_key(a) == _temporal_key(b)


def _has_layer_tag(scope: list[str]) -> bool:
    return any(tag.startswith("layer:") for tag in scope)


def _validate_scope_override(scope: list[str]) -> None:
    if scope and not _has_layer_tag(scope):
        raise InvalidDreamScope("Explicit dream scope must include a layer:* tag")


async def _resolve_bank_config(
    memory: "MemoryEngine", bank_id: str, request_context: "RequestContext | None"
) -> Any:
    resolver = getattr(memory, "_config_resolver", None)
    if resolver is None:
        return get_config()
    return await resolver.resolve_full_config(bank_id, request_context)


class _DSU:
    """Union-find over observation ids to coalesce candidate edges into clusters."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self._parent.setdefault(x, x)
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        # path compression
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # keep the lexicographically smaller id as the root (survivor heuristic)
            lo, hi = (ra, rb) if ra < rb else (rb, ra)
            self._parent[hi] = lo

    def clusters(self) -> list[list[str]]:
        groups: dict[str, list[str]] = {}
        for node in list(self._parent):
            groups.setdefault(self.find(node), []).append(node)
        return [sorted(members) for members in groups.values() if len(members) > 1]


def _scope_predicate(alias: str, scope: list[str], param: str) -> str:
    """SQL predicate restricting ``alias`` to one scope.

    Non-empty scope -> ``tags @> $param`` (the candidate's tags must contain all
    of the scope's tags; mirrors the consolidator's ``tags_match="all_strict"``).
    Empty scope -> ``cardinality(tags) = 0`` (the untagged / "shared" scope).
    The empty-array ``@>`` form is deliberately NOT used: ``tags @> '{}'`` is true
    for EVERY row, which would compare across the whole bank and break isolation.
    """
    prefix = f"{alias}." if alias else ""
    if scope:
        return f"{prefix}tags @> {param}::varchar[]"
    return f"cardinality({prefix}tags) = 0"


# ---------------------------------------------------------------------------
# Archive table (idempotent; created at extension startup — no alembic)
# ---------------------------------------------------------------------------
async def ensure_archive_table(memory: "MemoryEngine") -> None:
    """Create the side archive table if missing.

    ``LIKE memory_units INCLUDING DEFAULTS`` clones the live columns on first
    creation. Existing archives are then synced against current source columns so
    later ``memory_units``/``observation_history`` additions do not break the
    explicit archive INSERT column lists.
    """
    pool = await memory._get_pool()
    arch = fq_table(ARCHIVE_TABLE)
    arch_hist = fq_table(ARCHIVE_HISTORY_TABLE)
    mu = fq_table("memory_units")
    obs_hist = fq_table("observation_history")
    async with acquire_with_retry(pool) as conn:
        await conn.execute(f"CREATE TABLE IF NOT EXISTS {arch} (LIKE {mu} INCLUDING DEFAULTS)")
        await _sync_archive_columns(conn, mu, arch)
        await conn.execute(f"ALTER TABLE {arch} ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ DEFAULT now()")
        await conn.execute(f"ALTER TABLE {arch} ADD COLUMN IF NOT EXISTS merged_into_id UUID")
        await conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_dreaming_arch_bank ON {arch} (bank_id, archived_at)"
        )
        await conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_dreaming_arch_merged_into ON {arch} (merged_into_id)"
        )
        await conn.execute(f"CREATE TABLE IF NOT EXISTS {arch_hist} (LIKE {obs_hist} INCLUDING DEFAULTS)")
        await _sync_archive_columns(conn, obs_hist, arch_hist)
        await conn.execute(
            f"ALTER TABLE {arch_hist} ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ DEFAULT now()"
        )
        await conn.execute(f"ALTER TABLE {arch_hist} ADD COLUMN IF NOT EXISTS merged_into_id UUID")
        await conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_dreaming_arch_hist_obs ON {arch_hist} (observation_id, archived_at)"
        )
        await conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_dreaming_arch_hist_merged_into ON {arch_hist} (merged_into_id)"
        )
    logger.info("[DREAM] archive tables %s and %s ready", arch, arch_hist)


# ---------------------------------------------------------------------------
# Scope discovery
# ---------------------------------------------------------------------------
async def discover_scopes(conn: "Connection", bank_id: str, max_scopes: int) -> list[list[str]]:
    """Distinct non-empty tag-sets among this bank's observations = the scopes.

    Each distinct ``tags`` array is one write-scope (day/week/month/etc.). The
    untagged scope is excluded from discovery (it would otherwise widen to the
    whole bank via ``@>`` semantics); pass ``scope=[]`` explicitly to reduce it.
    """
    mu = fq_table("memory_units")
    rows = await conn.fetch(
        f"""
        SELECT DISTINCT tags
        FROM {mu}
        WHERE bank_id = $1 AND fact_type = 'observation'
          AND tags IS NOT NULL AND cardinality(tags) > 0
        ORDER BY tags
        """,
        bank_id,
    )
    scopes = [list(r["tags"]) for r in rows]
    if max_scopes and max_scopes > 0:
        scopes = scopes[:max_scopes]
    return scopes


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
async def _fetch_scope_rows(conn: "Connection", bank_id: str, scope: list[str]) -> list[dict[str, Any]]:
    """All observation rows in one scope (read-only).

    Returns id + text + temporal fields so detection AND planning can run entirely
    from this in-memory snapshot — the pooled connection is then released before any
    LLM adjudication, and the authoritative re-read happens under lock at apply time.
    """
    mu = fq_table("memory_units")
    pred = _scope_predicate("", scope, "$2")
    params: list[Any] = [bank_id]
    if scope:
        params.append(scope)
    rows = await conn.fetch(
        f"""
        SELECT id::text AS id, text, event_date, occurred_start, occurred_end, mentioned_at
        FROM {mu}
        WHERE bank_id = $1 AND fact_type = 'observation' AND {pred}
        """,
        *params,
    )
    return [dict(r) for r in rows]


def _exact_text_edges(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Edges between observations whose whitespace-normalised text is identical.

    Case is preserved (``_norm_obs_text`` semantics) so this stays temporally
    lossless — it never collapses "TLS" with "tls" or two different dates.
    """
    by_norm: dict[tuple[str, tuple[Any, Any, Any, Any]], list[str]] = {}
    for r in rows:
        by_norm.setdefault((_norm_obs_text(r["text"]), _temporal_key(r)), []).append(r["id"])
    edges: list[tuple[str, str]] = []
    for ids in by_norm.values():
        if len(ids) > 1:
            ids_sorted = sorted(ids)
            anchor = ids_sorted[0]
            for other in ids_sorted[1:]:
                edges.append((anchor, other))
    return edges


async def _knn_edges(
    conn: "Connection", bank_id: str, scope: list[str], k: int, threshold: float
) -> list[tuple[str, str]]:
    """Per-scope LATERAL k-NN edges over observation embeddings.

    Over-fetch ``LIMIT = max(k*5, 100)`` per anchor (config/request ``k`` = the
    desired neighbour count "want"), keep pairs at/above ``threshold`` cosine
    similarity (``1 - (a.embedding <=> b.embedding) >= threshold``), canonicalised
    with ``a.id < b.id``.
    """
    mu = fq_table("memory_units")
    lateral_limit = max(int(k) * 5, 100)
    a_pred = _scope_predicate("a", scope, "$2")
    b_pred = _scope_predicate("b", scope, "$2")
    params: list[Any] = [bank_id]
    if scope:
        params.append(scope)
    rows = await conn.fetch(
        f"""
        SELECT a.id::text AS a_id, nn.id::text AS b_id, nn.dist AS dist
        FROM {mu} a
        LEFT JOIN LATERAL (
            SELECT b.id, (a.embedding <=> b.embedding) AS dist
            FROM {mu} b
            WHERE b.fact_type = 'observation' AND b.bank_id = $1 AND {b_pred}
              AND b.id <> a.id AND b.embedding IS NOT NULL
            ORDER BY a.embedding <=> b.embedding
            LIMIT {lateral_limit}
        ) nn ON true
        WHERE a.fact_type = 'observation' AND a.bank_id = $1 AND {a_pred}
          AND a.embedding IS NOT NULL
        """,
        *params,
    )
    edges: set[tuple[str, str]] = set()
    for r in rows:
        b_id = r["b_id"]
        if b_id is None or r["dist"] is None:
            continue
        sim = 1.0 - float(r["dist"])
        if sim < threshold:
            continue
        a_id = r["a_id"]
        if a_id == b_id:
            continue
        lo, hi = (a_id, b_id) if a_id < b_id else (b_id, a_id)
        edges.add((lo, hi))
    return list(edges)


# ---------------------------------------------------------------------------
# Adjudication (LLM) — runs OUTSIDE any row lock
# ---------------------------------------------------------------------------
async def _adjudicate(dedup_llm_config: Any, new_text: str, existing_text: str) -> _DedupDecision:
    """Focused 1-by-1 merge/keep verdict, mirroring ``_dedup_adjudicate``.

    Uses ``_DEDUP_PROMPT`` verbatim and the SAME consolidation LLM config, so the
    temporal keep-criterion ("different dates/times are DISTINCT occurrences —
    keep both") applies exactly as in core consolidation.
    """
    return await dedup_llm_config.call(
        messages=[{"role": "user", "content": _DEDUP_PROMPT.format(new=new_text, existing=existing_text)}],
        response_format=_DedupDecision,
        scope=_DREAM_LLM_OP,
    )


async def _plan_cluster(
    dedup_llm_config: Any,
    scope: list[str],
    member_ids: list[str],
    rows_by_id: dict[str, dict[str, Any]],
) -> ClusterPlan | None:
    """Build a merge plan for one candidate cluster (LLM-gated; NO DB connection).

    Works from the in-memory scope snapshot (``rows_by_id``) so adjudication never
    holds a pooled connection during LLM latency. Survivor = lowest id. For each
    other member: an exact whitespace-normalised text match auto-merges (lossless,
    no LLM); otherwise the LLM adjudicates the member against the *current* survivor
    text (pairwise for N>2). Only members the LLM returns ``action="merge"`` (or
    exact-text duplicates) are folded in. Returns ``None`` when nothing merges.
    """
    by_id = {mid: rows_by_id[mid] for mid in member_ids if mid in rows_by_id}
    if len(by_id) < 2:
        return None
    ordered_ids = sorted(by_id)
    survivor_id = ordered_ids[0]
    survivor = by_id[survivor_id]

    merged_text = survivor["text"]
    members: list[MemberView] = [
        MemberView(
            id=survivor_id,
            text=survivor["text"],
            occurred_start=_iso(survivor["occurred_start"]),
            occurred_end=_iso(survivor["occurred_end"]),
            mentioned_at=_iso(survivor["mentioned_at"]),
            role="survivor",
        )
    ]
    merged_member_ids: list[str] = []

    for mid in ordered_ids[1:]:
        m = by_id[mid]
        view = MemberView(
            id=mid,
            text=m["text"],
            occurred_start=_iso(m["occurred_start"]),
            occurred_end=_iso(m["occurred_end"]),
            mentioned_at=_iso(m["mentioned_at"]),
            role="kept",
        )
        if not _same_temporal(survivor, m):
            view.reason = "kept (temporal columns differ)"
        elif _norm_obs_text(m["text"]) == _norm_obs_text(merged_text):
            view.role = "exact-text"
            view.reason = "exact whitespace-normalised text duplicate"
            merged_member_ids.append(mid)
        else:
            try:
                decision = await _adjudicate(dedup_llm_config, new_text=m["text"], existing_text=merged_text)
            except Exception as exc:  # conservative: on any LLM failure, keep distinct
                logger.warning("[DREAM] adjudication failed for %s vs %s: %s", mid[:8], survivor_id[:8], exc)
                view.reason = f"kept (adjudication error: {exc})"
                members.append(view)
                continue
            if decision.action == "merge":
                view.role = "semantic"
                view.reason = decision.reason or "LLM merge"
                merged_text = (decision.text or "").strip() or merged_text
                merged_member_ids.append(mid)
            else:
                view.reason = decision.reason or "LLM keep (temporally/semantically distinct)"
        members.append(view)

    if not merged_member_ids:
        return None
    return ClusterPlan(
        scope=scope,
        survivor_id=survivor_id,
        merged_text=merged_text,
        members=members,
        merged_member_ids=merged_member_ids,
    )


async def _rebase_merge_under_lock(
    dedup_llm_config: Any,
    survivor_row: dict[str, Any],
    live_members: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Recompute the merge from the locked CURRENT rows.

    Detection and initial adjudication intentionally run without row locks. Apply
    must not blindly write that stale text, because overlapping scopes can update
    the same survivor between detection and apply. This function makes the locked
    survivor text the new base, then re-adjudicates each still-live member against
    that current text before we embed and update.
    """

    merged_text = survivor_row["text"]
    merge_members: list[dict[str, Any]] = []
    for member in sorted(live_members, key=lambda r: r["id"]):
        mid = member["id"]
        if not _same_temporal(survivor_row, member):
            logger.info("[DREAM] kept %s during apply rebase; temporal columns differ", mid[:8])
            continue
        if _norm_obs_text(member["text"]) == _norm_obs_text(merged_text):
            merge_members.append(member)
            continue
        try:
            decision = await _adjudicate(dedup_llm_config, new_text=member["text"], existing_text=merged_text)
        except Exception as exc:
            logger.warning("[DREAM] apply rebase adjudication failed for %s: %s", mid[:8], exc)
            continue
        if decision.action == "merge":
            merged_text = (decision.text or "").strip() or merged_text
            merge_members.append(member)
        else:
            logger.info(
                "[DREAM] kept %s during apply rebase; %s",
                mid[:8],
                decision.reason or "LLM keep",
            )
    return merged_text, merge_members


# ---------------------------------------------------------------------------
# Apply (one transaction per cluster; idempotent + resumable)
# ---------------------------------------------------------------------------
async def _apply_cluster(
    memory: "MemoryEngine",
    dedup_llm_config: Any,
    bank_id: str,
    plan: ClusterPlan,
    config: Any,
) -> int:
    """Persist one cluster merge atomically. Returns the number of rows archived.

    Steps (mirrors ``_execute_update_action`` + the consolidator's atomic append):
      1. ``SELECT ... FOR UPDATE NOWAIT`` the survivor + members (lost-update guard;
         abort-on-conflict + retry).
      2. Rebase the merge onto the locked survivor text and still-live members.
      3. Re-embed that final text so text and vector match.
      4. Append history snapshot (``_append_observation_history``) before overwrite.
      5. Atomic array-append of source ids / tags into the survivor, temporal
         extents, event_date, and the re-embedded vector.
      6. Archive member ``observation_history`` rows and memory rows, then delete
         them from ``memory_units``. NEVER a bare hard-delete.
    """
    mu = fq_table("memory_units")
    arch = fq_table(ARCHIVE_TABLE)
    arch_hist = fq_table(ARCHIVE_HISTORY_TABLE)
    obs_hist = fq_table("observation_history")

    survivor_uuid = uuid.UUID(plan.survivor_id)
    member_uuids = [uuid.UUID(m) for m in plan.merged_member_ids]
    all_uuids = [survivor_uuid] + member_uuids

    pool = await memory._get_pool()

    for attempt in range(_APPLY_RETRIES):
        try:
            async with acquire_with_retry(pool) as conn:
                async with conn.transaction():
                    # (1) Re-read under lock in a deterministic id order (NOWAIT ->
                    # abort on contention rather than block, so a busy scope can't
                    # stall cross-scope parallelism).
                    locked = await conn.fetch(
                        f"""
                        SELECT id::text AS id, text, tags, source_memory_ids, proof_count,
                               event_date, occurred_start, occurred_end, mentioned_at
                        FROM {mu}
                        WHERE id = ANY($1::uuid[]) AND bank_id = $2 AND fact_type = 'observation'
                        ORDER BY id
                        FOR UPDATE NOWAIT
                        """,
                        all_uuids,
                        bank_id,
                    )
                    locked_by_id = {r["id"]: dict(r) for r in locked}
                    survivor_row = locked_by_id.get(plan.survivor_id)
                    if survivor_row is None:
                        logger.info("[DREAM] survivor %s vanished before apply; skip", plan.survivor_id[:8])
                        return 0

                    live_members = [locked_by_id[m] for m in plan.merged_member_ids if m in locked_by_id]
                    if not live_members:
                        logger.info("[DREAM] all merge targets for %s vanished; skip", plan.survivor_id[:8])
                        return 0

                    # (2) Rebase against CURRENT locked text/members. This also
                    # drops members that vanished, changed into a non-duplicate, or
                    # now differ in temporal columns.
                    final_text, merge_members = await _rebase_merge_under_lock(
                        dedup_llm_config, survivor_row, live_members
                    )
                    if not merge_members:
                        logger.info("[DREAM] no members survived apply rebase for %s; skip", plan.survivor_id[:8])
                        plan.merged_member_ids = []
                        return 0
                    plan.merged_text = final_text
                    plan.merged_member_ids = [r["id"] for r in merge_members]
                    live_members = merge_members
                    live_member_uuids = [uuid.UUID(r["id"]) for r in live_members]

                    # (3) Embed the exact text we are about to store.
                    embs = await embedding_utils.generate_embeddings_batch(memory.embeddings, [final_text])
                    embedding_str = str(embs[0]) if embs else None
                    if embedding_str is None:
                        logger.warning(
                            "[DREAM] empty embedding for survivor %s; skipping cluster",
                            plan.survivor_id[:8],
                        )
                        return 0

                    # Union the to-be-archived members' source facts + tags into the
                    # survivor. Observations carry source FACT ids, not observation
                    # ids, so we never add a member's own id as a source.
                    new_source_ids: list[uuid.UUID] = []
                    new_tags: list[str] = []
                    event_dates: list[datetime] = []
                    occ_starts: list[datetime] = []
                    occ_ends: list[datetime] = []
                    mentioned: list[datetime] = []
                    for r in live_members:
                        new_source_ids.extend(r["source_memory_ids"] or [])
                        new_tags.extend(r["tags"] or [])
                        if r["event_date"] is not None:
                            event_dates.append(r["event_date"])
                        if r["occurred_start"] is not None:
                            occ_starts.append(r["occurred_start"])
                        if r["occurred_end"] is not None:
                            occ_ends.append(r["occurred_end"])
                        if r["mentioned_at"] is not None:
                            mentioned.append(r["mentioned_at"])

                    event_date = min(event_dates) if event_dates else None
                    occ_start = min(occ_starts) if occ_starts else None
                    occ_end = max(occ_ends) if occ_ends else None
                    mentioned_at = max(mentioned) if mentioned else None

                    # (4) History snapshot BEFORE overwrite. Temporal fields are
                    # serialised to ISO strings — the snapshot is json.dumps'd with
                    # no datetime handler, so raw datetimes would raise TypeError.
                    if config.enable_observation_history:
                        snapshot = _ObservationHistorySnapshot(
                            previous_text=survivor_row["text"],
                            previous_tags=list(survivor_row["tags"] or []),
                            previous_occurred_start=_iso(survivor_row["occurred_start"]),
                            previous_occurred_end=_iso(survivor_row["occurred_end"]),
                            previous_mentioned_at=_iso(survivor_row["mentioned_at"]),
                            new_source_memory_ids=[str(x) for x in new_source_ids],
                        )
                        await _append_observation_history(
                            conn, bank_id, plan.survivor_id, snapshot, config.observation_history_max_entries
                        )

                    # (5) Atomic read-modify-write append onto the survivor's CURRENT
                    # values (a concurrent same-scope writer's links/tags survive).
                    await conn.execute(
                        f"""
                        UPDATE {mu}
                        SET text = $1,
                            embedding = $2::vector,
                            source_memory_ids = (
                                SELECT array_agg(DISTINCT e) FROM unnest(source_memory_ids || $3::uuid[]) e
                            ),
                            proof_count = (
                                SELECT count(DISTINCT e) FROM unnest(source_memory_ids || $3::uuid[]) e
                            ),
                            tags = (
                                SELECT array_agg(DISTINCT e) FROM unnest(tags || $4::varchar[]) e
                            ),
                            updated_at = now(),
                            event_date = CASE
                                WHEN event_date IS NULL THEN $6
                                WHEN $6 IS NULL THEN event_date
                                ELSE LEAST(event_date, $6)
                            END,
                            occurred_start = CASE
                                WHEN occurred_start IS NULL THEN $7
                                WHEN $7 IS NULL THEN occurred_start
                                ELSE LEAST(occurred_start, $7)
                            END,
                            occurred_end = CASE
                                WHEN occurred_end IS NULL THEN $8
                                WHEN $8 IS NULL THEN occurred_end
                                ELSE GREATEST(occurred_end, $8)
                            END,
                            mentioned_at = CASE
                                WHEN mentioned_at IS NULL THEN $9
                                WHEN $9 IS NULL THEN mentioned_at
                                ELSE GREATEST(mentioned_at, $9)
                            END
                        WHERE id = $5 AND bank_id = $10 AND fact_type = 'observation'
                        """,
                        final_text,
                        embedding_str,
                        new_source_ids,
                        new_tags,
                        survivor_uuid,
                        event_date,
                        occ_start,
                        occ_end,
                        mentioned_at,
                        bank_id,
                    )

                    # (6) Archive dependent history rows before deleting members so
                    # the ON DELETE CASCADE cannot erase lineage. All archive INSERTs
                    # enumerate source columns explicitly; no positional ``m.*``.
                    memory_columns = await _table_column_names(conn, mu)
                    memory_archive_columns = memory_columns + ["archived_at", "merged_into_id"]
                    history_columns = await _table_column_names(conn, obs_hist)
                    history_archive_columns = history_columns + ["archived_at", "merged_into_id"]
                    await conn.execute(
                        f"""
                        INSERT INTO {arch_hist} ({_column_list(history_archive_columns)})
                        SELECT {_column_list(history_columns, "h")}, now(), $2::uuid
                        FROM {obs_hist} h
                        WHERE h.observation_id = ANY($1::uuid[]) AND h.bank_id = $3
                        """,
                        live_member_uuids,
                        survivor_uuid,
                        bank_id,
                    )
                    await conn.execute(
                        f"""
                        INSERT INTO {arch} ({_column_list(memory_archive_columns)})
                        SELECT {_column_list(memory_columns, "m")}, now(), $2::uuid
                        FROM {mu} m
                        WHERE m.id = ANY($1::uuid[]) AND m.bank_id = $3 AND m.fact_type = 'observation'
                        """,
                        live_member_uuids,
                        survivor_uuid,
                        bank_id,
                    )
                    await conn.execute(
                        f"""
                        DELETE FROM {mu}
                        WHERE id = ANY($1::uuid[]) AND bank_id = $2 AND fact_type = 'observation'
                        """,
                        live_member_uuids,
                        bank_id,
                    )
                    logger.info(
                        "[DREAM] merged %d observation(s) into %s (scope=%s)",
                        len(live_members),
                        plan.survivor_id[:8],
                        plan.scope,
                    )
                    return len(live_members)
        except Exception as exc:
            if _is_conflict(exc) and attempt < _APPLY_RETRIES - 1:
                await asyncio.sleep(0.2 * (attempt + 1))
                continue
            if _is_conflict(exc):
                logger.warning("[DREAM] cluster %s still contended after retries; skip", plan.survivor_id[:8])
                return 0
            raise
    return 0


# ---------------------------------------------------------------------------
# Per-scope orchestration
# ---------------------------------------------------------------------------
async def reduce_scope(
    memory: "MemoryEngine",
    dedup_llm_config: Any,
    consolidation_config: Any,
    bank_id: str,
    scope: list[str],
    *,
    k: int,
    threshold: float,
    apply: bool,
) -> ScopeResult:
    """Detect + adjudicate (+ apply) within ONE scope. Idempotent / resumable."""
    pool = await memory._get_pool()
    try:
        # Detection holds a (lock-free) read connection only briefly; it is released
        # before any LLM adjudication runs.
        async with acquire_with_retry(pool) as conn:
            rows = await _fetch_scope_rows(conn, bank_id, scope)
            scanned = len(rows)
            if scanned < 2:
                return ScopeResult(scope=scope, observations_scanned=scanned, clusters_proposed=0,
                                   observations_merged=0)
            rows_by_id = {r["id"]: r for r in rows}
            dsu = _DSU()
            for a, b in _exact_text_edges(rows):
                dsu.union(a, b)
            for a, b in await _knn_edges(conn, bank_id, scope, k, threshold):
                dsu.union(a, b)
            candidate_clusters = dsu.clusters()

        # Adjudicate connection-free (the apply path re-reads authoritatively under lock).
        plans: list[ClusterPlan] = []
        for member_ids in candidate_clusters:
            plan = await _plan_cluster(dedup_llm_config, scope, member_ids, rows_by_id)
            if plan is not None:
                plans.append(plan)

        merged_total = 0
        if apply:
            for plan in plans:
                archived = await _apply_cluster(memory, dedup_llm_config, bank_id, plan, consolidation_config)
                plan.applied = archived > 0
                plan.archived_count = archived
                merged_total += archived
        else:
            # dry-run: report the proposed merge count without writing anything.
            merged_total = sum(len(p.merged_member_ids) for p in plans)

        return ScopeResult(
            scope=scope,
            observations_scanned=scanned,
            clusters_proposed=len(plans),
            observations_merged=merged_total,
            clusters=plans,
        )
    except Exception as exc:
        logger.exception("[DREAM] scope %s failed: %s", scope, exc)
        return ScopeResult(
            scope=scope, observations_scanned=0, clusters_proposed=0, observations_merged=0, error=str(exc)
        )


# ---------------------------------------------------------------------------
# Bank-level entry point
# ---------------------------------------------------------------------------
async def reduce_bank(
    memory: "MemoryEngine",
    bank_id: str,
    *,
    dcfg: DreamingConfig,
    request_context: "RequestContext | None" = None,
    mode: str = "dry-run",
    scope_override: list[str] | None = None,
    max_scopes: int | None = None,
    k: int | None = None,
    parallelism: int | None = None,
) -> DreamReport:
    """Run the dream reduce across one bank's scopes.

    ``mode``: ``"dry-run"`` (default; writes NOTHING) or ``"apply"``. Disjoint
    scopes are processed concurrently, bounded by ``parallelism``.
    """
    apply = mode == "apply"
    eff_k = dcfg.k if k is None else k
    eff_parallelism = max(1, dcfg.parallelism if parallelism is None else parallelism)
    eff_max_scopes = dcfg.max_scopes if max_scopes is None else max_scopes

    if scope_override is not None:
        _validate_scope_override(scope_override)

    if apply:
        await ensure_archive_table(memory)

    # The SAME consolidation LLM config the core consolidator uses (Modal nemotron),
    # wrapped with a per-bank trace context labelled ``dreaming_reduce``.
    consolidation_config = await _resolve_bank_config(memory, bank_id, request_context)
    dedup_llm_config = memory._consolidation_llm_config.with_config(
        consolidation_config, bank_id=bank_id, operation=_DREAM_LLM_OP
    )

    pool = await memory._get_pool()
    if scope_override is not None:
        scopes = [scope_override]
    else:
        async with acquire_with_retry(pool) as conn:
            scopes = await discover_scopes(conn, bank_id, eff_max_scopes)

    sem = asyncio.Semaphore(eff_parallelism)

    async def _run(scope: list[str]) -> ScopeResult:
        async with sem:
            return await reduce_scope(
                memory,
                dedup_llm_config,
                consolidation_config,
                bank_id,
                scope,
                k=eff_k,
                threshold=dcfg.reduce_threshold,
                apply=apply,
            )

    results = await asyncio.gather(*(_run(s) for s in scopes)) if scopes else []
    return DreamReport(
        mode=mode,
        bank_id=bank_id,
        scopes_scanned=len(results),
        clusters_proposed=sum(r.clusters_proposed for r in results),
        observations_merged=sum(r.observations_merged for r in results),
        scopes=list(results),
    )


async def list_bank_ids(memory: "MemoryEngine") -> list[str]:
    """All bank ids in the current schema (used by the background scheduler)."""
    pool = await memory._get_pool()
    banks = fq_table("banks")
    async with acquire_with_retry(pool) as conn:
        rows = await conn.fetch(f"SELECT bank_id FROM {banks} ORDER BY bank_id")
    return [r["bank_id"] for r in rows]
