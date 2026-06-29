# hindsight_dreaming

An **additive** Hindsight extension that performs an async *"dreaming"* dedup-reduce
over already-committed **observations**. It does **not** modify any core file — it
imports and reuses the core consolidator's dedup machinery, so a dream merge talks
to the **same** consolidation LLM (Modal nemotron) with the **same**
temporally-conservative prompt the engine already uses.

## Governing principle — temporal conservatism (Hindsight ≠ Honcho)

This is a daily-journaling, temporally-resolved corpus. The reduce **never** merges
across distinct dates / times / numbers / negations / entities. Embedding similarity
is a **recall-only** signal; the LLM (`_DEDUP_PROMPT`, which contains
*"Two observations of the same kind of event on different dates or times are DISTINCT
occurrences — keep both"*) is the **precision gate**. The day/week/month write-scopes
already isolate cross-time states, so the reduce only ever compares observations
**within one scope** (`tags @> $scope`).

## What it reuses from core (imported verbatim, never re-implemented)

From `hindsight_api.engine.consolidation.consolidator`:
- `_DEDUP_PROMPT` — the merge/keep prompt (temporal keep-criterion) used for every 2-node decision.
- `_DedupDecision` — the `action ∈ {merge, keep}` + synthesized `text` + `reason` schema.
- `_norm_obs_text` — case-preserving, whitespace-collapsing, **temporally lossless** normaliser (the cheap exact-text pre-pass).
- `_append_observation_history` / `_ObservationHistorySnapshot` — pre-overwrite history snapshot.

From the rest of the engine:
- `embedding_utils.generate_embeddings_batch(memory.embeddings, …)` — the **same** embedder, so the re-embedded survivor vector matches the stored text.
- `memory._consolidation_llm_config.with_config(…)` — the **same** consolidation LLM.
- `fq_table(…)` — schema-qualified table names; `acquire_with_retry(pool)` — pooled connections.

## Files

| File | Purpose |
| --- | --- |
| `__init__.py` | Re-exports `DreamingHttpExtension`, `DreamingMCPExtension`, `DreamingConfig`. |
| `config.py` | `DreamingConfig` read from `HINDSIGHT_API_DREAM_*` via `os.getenv` (same pattern as core `config.py`). |
| `reduce.py` | The reduce engine: archive DDL, scope discovery, exact-text + LATERAL k-NN detection, LLM adjudication, the per-cluster merge transaction, and the background scheduler entry points. |
| `dreaming.py` | `DreamingHttpExtension` (`POST /ext/dream`) + `DreamingMCPExtension` (`dream` tool) + Pydantic request/response models + scheduler wiring. |

## Loading (env vars)

```bash
# HTTP endpoint (POST /ext/dream)
HINDSIGHT_API_HTTP_EXTENSION=hindsight_dreaming:DreamingHttpExtension

# Optional MCP tool ("dream")
HINDSIGHT_API_MCP_EXTENSION=hindsight_dreaming:DreamingMCPExtension
```

The package must be importable (`PYTHONPATH` includes
`/home/ken/workspace/hindsight-consolidation/extensions`, or pip-install it). Mirror the
docs' deployment patterns:

```dockerfile
FROM vectorize/hindsight-api:latest
COPY hindsight_dreaming /app/hindsight_dreaming
ENV PYTHONPATH=/app
ENV HINDSIGHT_API_HTTP_EXTENSION=hindsight_dreaming:DreamingHttpExtension
```

> Only **one** HTTP extension and **one** MCP extension can be loaded at a time (each
> from a single env var). If you need both the dream endpoint and another HTTP
> extension, compose them in a wrapper extension.

## Configuration (`HINDSIGHT_API_DREAM_*`)

| Env var | Default | Meaning |
| --- | --- | --- |
| `HINDSIGHT_API_DREAM_ENABLED` | `true` | Master switch (endpoint + scheduler). |
| `HINDSIGHT_API_DREAM_REDUCE_THRESHOLD` | `0.94` | Cosine-similarity floor for a k-NN candidate pair (`1 - dist ≥ threshold`). Recall gate only. |
| `HINDSIGHT_API_DREAM_INTERVAL_SECONDS` | `0` | Background apply-mode cadence. `0` = scheduler disabled. |
| `HINDSIGHT_API_DREAM_K` | `100` | Desired neighbours per anchor ("want"); LATERAL `LIMIT = max(k*5, 100)`. |
| `HINDSIGHT_API_DREAM_PARALLELISM` | `16` | Max scopes reduced concurrently. |
| `HINDSIGHT_API_DREAM_MAX_SCOPES` | `0` | Cap on scopes per run (`0` = unlimited). |

> These live in the `HINDSIGHT_API_DREAM_*` namespace and are read directly via
> `os.getenv` — the extension loader only folds `HINDSIGHT_API_HTTP_*` /
> `HINDSIGHT_API_MCP_*` vars into the `config` dict, so the dream knobs are kept in
> their own namespace and shared by both extensions.

## HTTP API

`POST /ext/dream`

```jsonc
{
  "mode": "dry-run",          // "dry-run" (default, writes NOTHING) | "apply"
  "bank_id": "user-42",       // required
  "scope": ["day:2026-06-27"],// optional: a specific tag-set; omit to discover ALL scopes; [] = untagged scope
  "max_scopes": 0,            // optional overrides of the config defaults
  "k": 100,
  "parallelism": 16
}
```

Response: per-scope, per-cluster review report — member ids + texts + dates, the chosen
survivor, the proposed merged text, the LLM reason, and (in apply mode) `applied` /
`archived_count`. **dry-run is the migration safety gate: it performs no memory
writes** (it does call the LLM to produce the proposed merged text + reason — the same
telemetry the core consolidator emits — but mutates no observations and creates no
archive rows).

`GET /ext/dream/config` returns the resolved config.

## How it works

### 1. Scope discovery
Distinct non-empty `tags` arrays among the bank's observations. Each is one
write-scope. The untagged scope is excluded from auto-discovery (it would widen to the
whole bank via `@>`); pass `scope: []` explicitly to reduce it.

### 2. Detection (per scope)
1. **Exact-text pre-pass** — group by `_norm_obs_text(text)` (case/whitespace only,
   temporally lossless); identical-text groups become edges. These auto-merge (no LLM —
   identical text loses no information).
2. **LATERAL k-NN** over observation embeddings:
   ```sql
   SELECT a.id, nn.id AS b_id, nn.dist
   FROM memory_units a
   LEFT JOIN LATERAL (
       SELECT b.id, (a.embedding <=> b.embedding) AS dist
       FROM memory_units b
       WHERE b.fact_type = 'observation' AND b.bank_id = $1 AND b.tags @> $2::varchar[]
         AND b.id <> a.id AND b.embedding IS NOT NULL
       ORDER BY a.embedding <=> b.embedding
       LIMIT max(k*5, 100)          -- over-fetch
   ) nn ON true
   WHERE a.fact_type = 'observation' AND a.bank_id = $1 AND a.tags @> $2::varchar[]
     AND a.embedding IS NOT NULL;
   ```
   Keep pairs with `1 - dist ≥ reduce_threshold` and `a.id < b.id`.
3. Exact-text + k-NN edges feed a **union-find** → candidate clusters.

### 3. Adjudication (LLM, OUTSIDE any row lock)
Survivor = lowest id. For each other member: an exact whitespace-normalised match
auto-merges; otherwise the LLM adjudicates the member vs the **current** survivor text
(pairwise for N>2) via `_DEDUP_PROMPT`/`_DedupDecision`. Only `action="merge"` (or
exact-text) members are folded in. On any LLM error the pair is **kept** (conservative).

### 4. Merge transaction (one per cluster; apply mode only)
Mirrors `_execute_update_action` + the consolidator's atomic array-append:
1. Re-embed the survivor from the merged text (outside the txn — minimises lock hold).
2. `SELECT … FOR UPDATE NOWAIT` survivor + members in id order (lost-update guard,
   abort-on-conflict + bounded retry on SQLSTATE `55P03`/`40001`/`40P01`); skip rows
   that vanished concurrently.
3. `_append_observation_history(...)` **before** overwrite (datetimes serialised to ISO).
4. Atomic append into the survivor:
   ```sql
   source_memory_ids = (SELECT array_agg(DISTINCT e) FROM unnest(source_memory_ids || $3::uuid[]) e),
   proof_count       = (SELECT count(DISTINCT e) FROM unnest(source_memory_ids || $3::uuid[]) e),
   tags              = (SELECT array_agg(DISTINCT e) FROM unnest(tags || $4::varchar[]) e),
   occurred_start = LEAST(...), occurred_end = GREATEST(...), mentioned_at = GREATEST(...),
   embedding = $2::vector, text = $1
   ```
5. **Archive then delete** the redundant rows — never a bare hard-delete:
   ```sql
   INSERT INTO dreaming_archived_observations
   SELECT m.*, now(), $survivor::uuid
   FROM memory_units m
   WHERE m.id = ANY($ids::uuid[]) AND m.bank_id = $bank AND m.fact_type = 'observation';
   DELETE FROM memory_units WHERE id = ANY($ids::uuid[]) AND bank_id = $bank AND fact_type = 'observation';
   ```

The whole pass is idempotent/resumable per scope, and disjoint scopes run concurrently
(bounded by `parallelism`).

### 5. Background scheduler (`on_startup`)
If `HINDSIGHT_API_DREAM_INTERVAL_SECONDS > 0`, a decoupled loop runs apply-mode reduce
across all banks every interval. Each iteration/bank is wrapped in try/except so a
failure can never crash the host app. Disabled (`0`) by default.

## Archive table DDL (idempotent; no alembic)

The table is created in `DreamingHttpExtension.on_startup` (per the brief), wrapped so
a DDL failure cannot crash the host; apply-mode reduce re-ensures it as a backstop for
the sub-app-mount case where `on_startup` does not fire. Dry-run never creates it.

```sql
CREATE TABLE IF NOT EXISTS "<schema>".dreaming_archived_observations
    (LIKE "<schema>".memory_units INCLUDING DEFAULTS);
ALTER TABLE "<schema>".dreaming_archived_observations ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ DEFAULT now();
ALTER TABLE "<schema>".dreaming_archived_observations ADD COLUMN IF NOT EXISTS merged_into_id UUID;
CREATE INDEX IF NOT EXISTS idx_dreaming_arch_bank       ON "<schema>".dreaming_archived_observations (bank_id, archived_at);
CREATE INDEX IF NOT EXISTS idx_dreaming_arch_merged_into ON "<schema>".dreaming_archived_observations (merged_into_id);
```

`LIKE memory_units INCLUDING DEFAULTS` clones every live column verbatim (the proven
idiom from core's `invalidated_memory_units`). CHECK constraints / FKs / indexes are
intentionally **not** cloned — this is cold audit storage, not a recall surface.

## Caveats / known limitations

- **Full row copy keeps the embedding** (per the brief). Two consequences of the
  positional `INSERT … SELECT m.*, now(), $survivor`:
  1. If a future **core migration adds a `memory_units` column**, the existing archive
     table won't gain it (`IF NOT EXISTS`), and the positional insert will mismatch —
     migrate/re-create the archive when core's schema changes.
  2. An **embedding-dimension change** that re-dimensions `memory_units.embedding` can
     trip a dimension mismatch on archive (exactly why core's `invalidated_memory_units`
     drops its embedding). If you adopt a dimension switch, drop `embedding` from the
     archive (`ALTER TABLE … DROP COLUMN embedding`) and from the `SELECT`.
- **`unit_entities` links are not archived** — a future "un-dream" restore would lose a
  member's entity associations (the survivor retains its own + inherits sources for
  graph traversal). The archive is for audit/recovery of the row, not a transactional
  un-merge.
- **Re-embedding the survivor** is an intentional divergence from core's create-time
  dedup (which keeps the old vector); here the merged text can differ enough that a
  fresh vector is the correct stored representation.
- **Scheduler schema scope:** outside an HTTP request there is no tenant schema in
  context, so the scheduler operates on the configured default schema
  (`HINDSIGHT_API_DATABASE_SCHEMA`, default `public`). On the single-schema deployment
  this is correct; multi-tenant deployments should drive apply-mode per-tenant via the
  HTTP endpoint instead.
- **Postgres only.** The merge path uses PG array ops (`unnest`/`array_agg`,
  `tags @> …::varchar[]`, `<=>`), matching the core dedup's PG-only guard.

## Validation

```bash
python3 -m py_compile config.py reduce.py dreaming.py __init__.py
```

No DB is touched and nothing is deployed by importing/compiling this package.
