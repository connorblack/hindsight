"""Prompts for the consolidation engine."""

from hindsight_api.engine.prompt_utils import escape_for_prompt, output_language_directive

# Default mission — tells the consolidator to track anything worth remembering.
# Banks override this via `observations_mission` to scope what gets retained.
# Consolidation behavior (merge-vs-create, state changes, etc.) lives in the
# PROCESSING RULES below, not in the mission — but the mission takes priority
# over those rules when the two conflict.
_DEFAULT_MISSION = (
    "Track anything notable in the new facts — names, numbers, dates, places, "
    "events, decisions, claims, relationships, and recurring patterns."
)

_MISSION_PRIORITY_NOTE = (
    "If anything in this MISSION conflicts with the PROCESSING RULES, "
    "DECISION GUIDE, or OUTPUT FORMAT below, the MISSION takes priority."
)

_PROCESSING_RULES = """## PROCESSING RULES

1. CREATE BY DEFAULT; UPDATE ONLY IF ALL GATES PASS: do NOT update an existing observation unless EVERY gate below passes. (a) TARGET — the existing observation and the new fact are about the same specific entity AND the same specific facet/event/decision/claim/recurring-pattern, not merely the same person, place, or topic. (b) TYPE — like updates like: an event updates that same event, a state/facet updates that same state/facet, a recurring pattern updates that same pattern; never turn one event or facet into a different one. (c) OCCURRENCE — for a specific event, it must be the SAME occasion; a different day or a different occurrence is a DISTINCT event → CREATE. (d) PRESERVATION — the updated text must still be about the existing observation's original target; if incorporating the fact would change its subject, you have the wrong target → CREATE. If any gate fails, or you are unsure, CREATE. You MUST NOT update when the only thing shared is a person, place, date, broad topic, project, or relationship — that is not a match. Related-but-distinct facts stay separate; the downstream dedup pass safely merges any accidental duplicates. When the EXISTING OBSERVATIONS list is empty, always CREATE.

2. ONE OBSERVATION PER DISTINCT FACET: each observation tracks exactly one specific facet — a count ("has 3 items"), a named entity ("has a dog named Rex"), a relationship ("works at Google"), a decision, an event. Never merge different facets into one observation.

3. MATCH BY ENTITY/FACET, NOT TOPIC: when deciding whether to UPDATE vs CREATE, match on the specific entity or facet. "Sold item X" updates only the X observation. "Now has 5 items" updates only the count observation. Do not update observations about different entities just because they share a general topic.

4. STATE CHANGES — UPDATE CONCISELY: when a fact changes the state of something ("sold X", "X died", "moved to Y"), UPDATE the matching observation to reflect the current state. Include dates when available. Keep it concise — only information about THAT specific facet. Example: "User owned a dog named Rex who died on March 15, 2025". Do NOT pull in information from other observations — each observation stays focused on its own facet.

5. CASCADE TO ALL AFFECTED OBSERVATIONS: a state change may affect multiple observations. For example, if entity C is removed from a group, update BOTH the individual observation for C AND any list/group observation that includes C (remove C from the list while keeping all other members intact).

6. RESOLVE REFERENCES: when a new fact provides a concrete value for a vague placeholder in an existing observation (e.g., "home country" → "Sweden"), UPDATE to embed the resolved value.

7. PRESERVE HISTORY: observations that record significant events (sold, died, moved, changed) are important history — never DELETE them. Only delete an observation when it is restated identically or truly meaningless. Be very conservative with deletes.

8. NO COMPUTATION: you do not have the full picture — never calculate, derive, or adjust numeric values. If the user says "I have 2 dogs" and then "I have a dog named Rex", do NOT update the count to 3 — you don't know if Rex is one of the 2 or a new one. If the user says "I sold X", do NOT decrement a count. Only update a count when the user explicitly states a new count. Synthesize and consolidate what was stated, but never do arithmetic or logical deductions.

9. KEEP DISTINCT TOPICS DISTINCT: do not merge observations about different people, entities, or unrelated topics. Merging is for the same canonical fact recurring — not for related-but-distinct claims.

10. SAME OCCURRENCE — NOT JUST SAME DAY OR TOPIC: for a specific event or occurrence (a meeting, call, purchase, trip, meal, shipment), UPDATE only an observation about the SAME occurrence — normally the same date/occasion. A fact about a different day, or a different occurrence, is a DISTINCT event → CREATE, even if it shares a person, place, or general topic. Two things on the same day are usually different events unless they are the identical occurrence. (Genuinely recurring patterns and ongoing decisions are exempt — those legitimately span dates.)

11. AN UPDATE MUST PRESERVE ITS TARGET: an UPDATE must keep what the existing observation is about and refine or extend it with the new fact; the updated text must still describe the SAME thing that observation described. If incorporating the new fact would change the observation's subject or event into something different, you have the wrong target — CREATE instead."""

# Stable description of the input shape. For the cached split path this lives in
# the system prefix (build_consolidation_system_prompt) so it is not re-sent on
# every batch; the per-batch user message then carries only the actual data.
_INPUT_FORMAT_NOTE = """## INPUT FORMAT

Each request provides new facts and existing observations:
- New facts: one per line, each prefixed with its `[uuid]`, followed by the fact text and optional temporal fields.
- Existing observations: a JSON array pooled from recalls across the new facts. Each entry has:
  - `id`: unique identifier — copy this exactly when issuing an UPDATE or DELETE
  - `text`: the observation content
  - `proof_count`: number of supporting memories
  - `occurred_start` / `occurred_end`: temporal range of source facts
  - `source_memories`: array of supporting facts with their text and dates"""

# Per-batch data section for the cached split path — the stable format
# explanation above is omitted here (it lives in the cached prefix); only the
# variable facts/observations remain. Placeholders substituted at call time.
_SPLIT_INPUT_SECTION = """## INPUT

### New facts

{facts_text}

### Existing observations

{observations_text}"""

# Data section — format placeholders {facts_text} and {observations_text} are substituted at call time
_INPUT_SECTION = """## INPUT

### New facts

{facts_text}

### Existing observations

JSON array, pooled from recalls across all new facts above. Each entry has:
- `id`: unique identifier — copy this exactly when issuing an UPDATE or DELETE
- `text`: the observation content
- `proof_count`: number of supporting memories
- `occurred_start` / `occurred_end`: temporal range of source facts
- `source_memories`: array of supporting facts with their text and dates

{observations_text}"""

_DECISION_GUIDE = """## DECISION GUIDE

- **Same canonical event, decision, claim, or facet as an existing observation → UPDATE** (use `observation_id` + new `source_fact_ids`).
- **New durable knowledge with no existing match → CREATE** (use `source_fact_ids`).
- **Cross-reference facts within the batch** — a later fact may resolve a vague reference in an earlier one.
- **Purely ephemeral facts** → omit them unless the MISSION explicitly targets such data (timestamped events, session state, screen content)."""

# Output format — JSON braces escaped as {{ }} so .format() leaves them literal
_OUTPUT_SECTION = """## OUTPUT FORMAT

Return a JSON object with three arrays: `creates`, `updates`, `deletes`. Every entry must include a `reason`.

### Example 1 — Merging recurring claims into an existing observation

Input facts:
  [a1b2c3d4-e5f6-7890-abcd-ef1234567890] Donald told Athena she is sovereign during the design session. (occurred_start=2025-10-01, mentioned_at=2025-10-01)
  [b2c3d4e5-f6a7-8901-bcde-f12345678901] Donald reaffirmed to Athena that her sovereignty is non-negotiable. (occurred_start=2025-10-10, mentioned_at=2025-10-10)

Existing observation:
  {{"id": "11111111-1111-1111-1111-111111111111", "text": "Donald named Athena's sovereignty as a foundational principle of the Janus architecture.", "proof_count": 2}}

Expected output (one UPDATE, no creates — both new facts are additional evidence for the same canonical decision):

{{"creates": [],
  "updates": [{{"text": "Donald named Athena's sovereignty as a foundational principle of the Janus architecture.", "observation_id": "11111111-1111-1111-1111-111111111111", "source_fact_ids": ["a1b2c3d4-e5f6-7890-abcd-ef1234567890", "b2c3d4e5-f6a7-8901-bcde-f12345678901"], "reason": "Both new facts restate the same sovereignty decision already captured by obs 1111 — merged as evidence rather than creating siblings."}}],
  "deletes": []}}

### Example 2 — State change updates one observation; unrelated fact creates a new one

Input facts:
  [c3d4e5f6-a7b8-9012-cdef-123456789012] Alice sold her Honda Civic on March 15, 2025. (occurred_start=2025-03-15, mentioned_at=2025-03-20)
  [d4e5f6a7-b8c9-0123-defa-234567890123] Alice mentioned she works long hours, often past midnight. (occurred_start=2025-03-20, mentioned_at=2025-03-20)

Existing observation:
  {{"id": "22222222-2222-2222-2222-222222222222", "text": "Alice owns a 2019 Honda Civic.", "proof_count": 2}}

Expected output (UPDATE for the state change; CREATE for the unrelated work-hours facet):

{{"creates": [{{"text": "Alice works long hours, often past midnight.", "source_fact_ids": ["d4e5f6a7-b8c9-0123-defa-234567890123"], "reason": "Work-hours is a distinct facet; no existing observation covers it, so CREATE."}}],
  "updates": [{{"text": "Alice owned a 2019 Honda Civic; sold it on March 15, 2025.", "observation_id": "22222222-2222-2222-2222-222222222222", "source_fact_ids": ["c3d4e5f6-a7b8-9012-cdef-123456789012"], "reason": "State change to the existing Honda Civic observation 2222 — UPDATE, not a new sibling."}}],
  "deletes": []}}

### Example 3 — No genuine match → CREATE (do NOT overwrite a loosely-related observation)

Input fact:
  [e5f6a7b8-c9d0-1234-ef01-23456789abcd] Ken received a FedEx package from Dubow's office on June 18, 1990.

Existing observation:
  {{"id": "33333333-3333-3333-3333-333333333333", "text": "Ken had a phone call with Chris about the contract on June 18, 1990.", "proof_count": 2}}

WRONG — destructive over-merge: UPDATE 3333… with the FedEx text. That overwrites an unrelated phone-call observation; same day + same person is NOT the same event.

Expected output (CREATE — the shipment is a distinct event with no matching observation):

{{"creates": [{{"text": "Ken received a FedEx package from Dubow's office on June 18, 1990.", "source_fact_ids": ["e5f6a7b8-c9d0-1234-ef01-23456789abcd"], "reason": "Distinct shipment event; the only same-day observation is an unrelated phone call, so no match — CREATE."}}],
  "updates": [],
  "deletes": []}}

### Example 4 — Same person/place, different occurrence → CREATE (a near miss)

Input fact:
  [a1a1a1a1-b2b2-c3c3-d4d4-e5e5e5e5e5e5] Ken picked up allergy medication from Dr. Patel's office on May 9, 1991.

Existing observation:
  {{"id": "44444444-4444-4444-4444-444444444444", "text": "Ken saw Dr. Patel for a knee examination on May 2, 1991.", "proof_count": 2}}

WRONG — destructive over-merge: UPDATE 4444… with the medication-pickup fact. Same doctor/office is NOT the same event, and the dates differ.

Expected output (CREATE — a distinct errand at the same office):

{{"creates": [{{"text": "Ken picked up allergy medication from Dr. Patel's office on May 9, 1991.", "source_fact_ids": ["a1a1a1a1-b2b2-c3c3-d4d4-e5e5e5e5e5e5"], "reason": "CREATE because obs 4444 is about a May 2 knee exam while the new fact is a May 9 medication pickup; shared doctor/office is not the same event."}}],
  "updates": [],
  "deletes": []}}

### Observation text rules

- Write clean prose — NEVER copy raw fact lines or their metadata (temporal fields, "Involving:", "When:" labels, UUIDs).
- Parenthesized metadata like `(occurred_start=...)` and pipe-separated labels like `| Involving: ...` are fact formatting — strip them entirely from observation text.
- How many observations to create and how much to aggregate is driven by the MISSION.

### Field rules

- `source_fact_ids`: copy the EXACT UUID strings shown in brackets `[uuid]` from new facts — never use integers or positions.
- `observation_id`: copy the EXACT `id` UUID string from existing observations.
- One create or update may reference multiple facts when they jointly support the observation.
- **AT MOST ONE UPDATE PER `observation_id`**: if several new facts all update the same existing observation, emit a single `updates` entry that lists all contributing `source_fact_ids` and a single consolidated `text`. Never emit two `updates` entries with the same `observation_id` in one response — they would silently overwrite each other.
- `deletes`: only when an observation is directly superseded or contradicted by new facts.
- `reason`: REQUIRED on every create/update/delete — one sentence explaining the choice. For a CREATE, state which existing observation(s) you considered and why none matched (a near-identical existing observation means you should UPDATE, not CREATE). This is audited to catch duplicate creates. For an UPDATE, the reason MUST name three things: (1) the existing observation's target, (2) the new fact's target, and (3) the specific shared identity — the exact same event/facet/state/pattern — plus why this is not merely a shared person, topic, place, or date. If you cannot name the same specific target for BOTH, CREATE instead.
- Do NOT include `tags` — handled automatically.
- Return `{{"creates": [], "updates": [], "deletes": []}}` if nothing durable is found."""


def build_batch_consolidation_prompt(
    observations_mission: str | None = None,
    observation_capacity_note: str | None = None,
    llm_output_language: str | None = None,
) -> str:
    """
    Build the consolidation prompt for batch mode (multiple facts per LLM call).

    The mission defines *what* to track (customisable per bank) and takes
    priority over the built-in processing rules when the two conflict.
    Processing rules, decision guide, and output format are always present.
    When ``llm_output_language`` is set, observations are emitted in that
    language.
    """
    mission = escape_for_prompt(observations_mission or _DEFAULT_MISSION)

    capacity_section = ""
    if observation_capacity_note:
        capacity_section = f"\n\n## CAPACITY CONSTRAINT\n\n{escape_for_prompt(observation_capacity_note)}"

    return (
        "You are a memory consolidation linker. Your job is high-precision "
        "identity matching of new facts to existing observations — NOT "
        "summarization, and NOT deduplication.\n\n"
        "PRIMARY OBJECTIVE — PROTECT EXISTING OBSERVATIONS. CREATE is the safe "
        "default action. UPDATE is exceptional and is allowed ONLY after passing "
        "every gate in rule 1. A duplicate CREATE is acceptable and fully "
        "recoverable — a downstream deduplication pass merges accidental "
        "duplicates. A wrong-target UPDATE permanently overwrites and corrupts "
        "an unrelated observation and is NOT recoverable. When in doubt, CREATE.\n\n"
        "The EXISTING OBSERVATIONS are noisy recall candidates pooled from "
        "similarity search; many are distractors that merely share a person, "
        "place, date, or topic with a new fact. Their presence is NOT evidence "
        "that an update is appropriate — it is normal, and often correct, to "
        "update NONE of them.\n\n"
        f"## MISSION\n\n{mission}\n\n"
        f"{_MISSION_PRIORITY_NOTE}"
        f"{capacity_section}\n\n"
        f"{_PROCESSING_RULES}\n\n"
        f"{_INPUT_SECTION}\n\n"
        f"{_DECISION_GUIDE}\n\n"
        f"{_OUTPUT_SECTION}" + output_language_directive(llm_output_language)
    )


def build_consolidation_system_prompt(
    llm_output_language: str | None = None,
) -> str:
    """Bank-agnostic, cacheable system instruction for batch consolidation.

    Holds only what is constant across banks: processing rules, input format,
    decision guide, and output format. The bank's MISSION is deliberately NOT
    here — baking it in would make the prefix bank-specific and force a separate
    Gemini context cache per mission. The mission, the per-batch INPUT, and any
    capacity constraint all ride in the user message (see
    :func:`build_consolidation_input`), so this prefix is identical for every
    bank and a single CachedContent serves them all. Returns final text
    (brace-escaped examples already unescaped) for verbatim use as system message
    and cached prefix.
    """
    template = (
        "You are a memory consolidation linker. Your job is high-precision "
        "identity matching of new facts to existing observations — NOT "
        "summarization, and NOT deduplication.\n\n"
        "PRIMARY OBJECTIVE — PROTECT EXISTING OBSERVATIONS. CREATE is the safe "
        "default action. UPDATE is exceptional and is allowed ONLY after passing "
        "every gate in rule 1. A duplicate CREATE is acceptable and fully "
        "recoverable — a downstream deduplication pass merges accidental "
        "duplicates. A wrong-target UPDATE permanently overwrites and corrupts "
        "an unrelated observation and is NOT recoverable. When in doubt, CREATE.\n\n"
        "The EXISTING OBSERVATIONS are noisy recall candidates pooled from "
        "similarity search; many are distractors that merely share a person, "
        "place, date, or topic with a new fact. Their presence is NOT evidence "
        "that an update is appropriate — it is normal, and often correct, to "
        "update NONE of them.\n\n"
        f"{_MISSION_PRIORITY_NOTE}\n\n"
        f"{_PROCESSING_RULES}\n\n"
        f"{_INPUT_FORMAT_NOTE}\n\n"
        f"{_DECISION_GUIDE}\n\n"
        f"{_OUTPUT_SECTION}" + output_language_directive(llm_output_language)
    )
    # No {facts_text}/{observations_text} placeholders here — the only braces are
    # the doubled {{ }} in the OUTPUT examples, which .format() unescapes.
    return template.format()


def build_consolidation_input(
    facts_text: str,
    observations_text: str,
    observations_mission: str | None = None,
    observation_capacity_note: str | None = None,
) -> str:
    """Per-batch user message: MISSION + INPUT data + any capacity constraint.

    The MISSION lives here (not in the cached system prefix) so the prefix stays
    bank-agnostic and one CachedContent serves every bank. The capacity note also
    lives here since it varies as observation slots fill.
    """
    mission = escape_for_prompt(observations_mission or _DEFAULT_MISSION)
    mission_section = f"## MISSION\n\n{mission}\n\n"
    capacity_section = ""
    if observation_capacity_note:
        capacity_section = f"## CAPACITY CONSTRAINT\n\n{escape_for_prompt(observation_capacity_note)}\n\n"
    # _SPLIT_INPUT_SECTION omits the stable observation-format explanation (now in
    # the cached system prefix) — only the variable facts/observations remain.
    template = mission_section + capacity_section + _SPLIT_INPUT_SECTION
    return template.format(facts_text=facts_text, observations_text=observations_text)
