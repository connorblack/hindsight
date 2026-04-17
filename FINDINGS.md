# Self-Improving Agent Skills: Findings

Research log from building and testing file-based procedural memory for OpenClaw agents (April 2026). Goal: understand where local file-based memory breaks and where an external system (like Hindsight) is genuinely needed — not assumed.

## Context

We built an `agent-memory` skill that gives an OpenClaw agent a `~/.agent-memory/<agent>/` directory where it maintains a wiki of everything it learned. Two file types: **knowledge files** (current-state, in-place updates) and **activity logs** (append-only, what was delivered). All files have mandatory evidence sections. Directory is git-tracked for version history.

Tested primarily with a "news-feed" agent that curates AI/ML news for the user.

Related work: [Memento-Skills (arXiv:2603.18743)](https://arxiv.org/abs/2603.18743) — "Let Agents Design Agents". Similar premise (skills as memory, read-write reflective learning), but their skills are executable code folders mutated by a judge, not wiki-style knowledge files.

---

## Architecture Options Tested

### Option A: Agent writes its own memory (synchronous, in-session)

The agent reads memory files at the start of each turn, does the task, then writes/updates memory files after responding.

**Setup:**
- Skill instructs the agent on file structure, naming, evidence format
- Two mandatory triggers: (1) knowledge updates when something durable is learned, (2) activity log entry after every task run
- Write order: respond → repair structural issues → update files → git commit
- Mandatory end-of-turn checklist to catch missed writes

**What worked:**
- Agent reads memory reliably and uses it to inform decisions (dedup, preferences, procedures)
- Activity log queries work well ("what Vercel items did you show me?")
- Evidence trail provides basic provenance
- Git history gives full diffs and rollback
- Index file (`_index.md`) helps with retrieval at moderate scale
- Agent's self-diagnosis of skill ambiguities was excellent — it identified gaps we hadn't seen

**What broke:**
1. **Unreliable execution of post-response writes.** The agent understood the write rules, agreed with them, then forgot to execute step 3/4 after generating the response. The LLM's natural stopping point is after the visible output — post-scripts get dropped. The checklist (`📝 Memory: [wrote: X | logged: Y | committed: Z]`) helps but is a band-aid: it catches the failure, doesn't prevent it.

2. **Latency.** Every turn pays 2-4 extra tool calls for memory I/O (read index, read relevant files, write updates, git commit). ~2-8 seconds of overhead the user can feel.

3. **Agent asks user about memory structure.** Without explicit instruction not to, the agent proposes file organization decisions to the user ("should I create a vercel-items.md?"). Memory should be internal — the user shouldn't know or care how it's organized. Required adding rule 11: "never surface memory structure to the user."

4. **Duplicate files.** Agent created `preferences.md` AND `news-feed-preferences.md` for the same topic. Required explicit dedup rules and immediate merge-on-discovery.

5. **Missing structural scaffolding.** Agent discovered `_index.md` was missing, noted it, but didn't fix it in the same turn. Required making repair mandatory in the same turn, not deferred.

6. **No cross-session pattern detection.** The agent only sees what it reads from files. If the user rejected hardware benchmarks across 4 separate sessions but the agent didn't read the right file each time, the pattern goes unnoticed. Each session is stateless except for explicit file reads.

**Fundamental limitation:** The agent is both the worker AND the memory system. When it's busy generating a complex response (10-item news feed with web searches), the "also update your notes" instruction competes for attention with the primary task. The primary task wins.

### Option B: Background LLM process (asynchronous, offline)

Not fully implemented, but designed. A separate script reads session transcripts from OpenClaw's JSONL files after each session, calls an LLM to extract what's wiki-worthy, updates the memory files, and commits.

**Why B is better in theory:**
- **Reliability:** A script that runs as a cron/hook doesn't forget steps. It always processes every turn.
- **No latency cost:** The user's turn isn't blocked by memory writes.
- **Full context:** The script sees the complete session transcript, not just the current turn. Can detect patterns across the session.
- **Batching:** Can process multiple turns at once, dedup, resolve contradictions in one pass.
- **Separation of concerns:** The agent does the task; the script maintains memory. Neither interferes with the other.

**Why B is hard without an external system:**
- **Cron/hook infrastructure is painful.** Setting up a reliable post-session hook that survives machine restarts, handles errors, retries on failure — that's a system, not a script. "Just run a cron job" is easy to say, hard to maintain.
- **LLM cost and coordination.** The script needs its own LLM call budget. If it uses the same model as the agent, you're doubling LLM cost. If it uses a cheaper model, synthesis quality drops.
- **Checkpoint management.** The script needs to track "which turns have I already processed" to avoid re-processing. That's a checkpoint file that can get out of sync.
- **File locking.** The agent might be reading memory files while the script is writing them. Race conditions on the wiki.
- **No semantic search.** Even with B, retrieving from 50+ memory files requires the agent to read the index and guess which files are relevant. No embeddings, no similarity search.

### Option C: Mechanical extraction (no LLM, regex/heuristic)

Designed but rejected for knowledge extraction. Works for activity logging (parse session transcript → extract delivered items → append to log) but can't extract preferences, rules, or procedures from natural language without LLM judgment.

**Verdict:** C is a valid sub-component of B (for the activity-log part specifically), not a standalone solution.

---

## Key Findings

### 1. Capture and synthesis must be separated

Asking the agent to both produce output AND maintain its memory in the same turn is unreliable. The capture (raw logging) must be infrastructure-level and deterministic. The synthesis (extracting knowledge from raw logs) can be LLM-driven but should happen asynchronously.

This is the single most important finding. It's also exactly what Hindsight's architecture does: auto-retain (deterministic hook) + consolidation (async LLM synthesis).

### 2. The agent is an excellent reader but an unreliable writer

Reading memory files and applying them to the task works well. The agent correctly deduplicates, applies preferences, follows procedures from memory. The failure is consistently on the write side — updating files after the task is done. Reads are pre-task (motivated by the task); writes are post-task (afterthought).

### 3. Evidence/provenance is valuable but expensive

The evidence trail (every fact cites a dated event) is useful for debugging "why does the agent think X". Git history adds full diffs. But maintaining evidence is another post-response write the agent can forget, and it adds ~30% more content to every file update.

### 4. File-based retrieval has a scale ceiling

With an index file, the agent can efficiently work with ~20-30 memory files. Beyond that, it needs to read the index, make relevance judgments, and selectively read — which adds latency and can miss relevant files. Embedding-based search (what Hindsight provides) removes this ceiling.

### 5. Memento-Skills' approach (skill = memory, agent mutates it) has the same write-reliability problem

Their Read-Write loop has the same structure: the agent acts, then reflects and rewrites the skill. They gate writes with a judge + unit tests + rollback — much heavier infrastructure than our evidence-and-commit approach, but it exists because the same failure mode (agent forgets/botches the write) exists for them too. Their solution: make the write a separate, guarded pipeline. Ours: post-response checklist. Theirs is more reliable but much more complex.

### 6. An external system's irreducible value is reliable capture + async synthesis

After testing all options, the value an external system (Hindsight, or anything like it) provides over files is:

| Capability | Files | External system |
|---|---|---|
| Reliable capture (never misses a turn) | ❌ Agent forgets | ✅ Hook-driven, deterministic |
| Async synthesis (no user-facing latency) | ❌ Blocks the turn | ✅ Background worker |
| Semantic search at scale | ❌ Index + grep | ✅ Embeddings + reranking |
| Cross-session pattern detection | ❌ Agent only sees what it reads | ✅ Sees all retained facts |
| Contradiction resolution | ❌ Agent must notice + fix | ✅ Consolidation pipeline |
| Provenance chain | ⚠️ Evidence section (manual) | ⚠️ Possible but not built yet |

What an external system does NOT provide better than files:
- **Transparency:** Files are more readable than a database
- **Simplicity:** Zero infrastructure for files
- **Agent autonomy:** The agent decides what matters in both cases
- **Offline/no-server:** Files work without any running service

### 7. The hybrid is probably the right answer

- **Files** for the agent's curated wiki (knowledge files, readable, transparent, git-tracked)
- **External system** for reliable capture (never miss a turn) + async synthesis (background LLM updates the wiki from captured turns) + semantic search (retrieve from the wiki at scale)

The file wiki becomes the *rendered output* of the external system's synthesis, not a competing artifact. The agent reads files; the system writes them. The agent can *also* write (for in-session corrections that can't wait for async), but the system is the primary writer.

---

## Open Questions

1. **Can the agent self-correct with just a checklist?** The `📝 Memory` checklist helps but it's unclear if it's reliable over 100+ sessions. Needs longer testing.

2. **What's the right latency for async synthesis?** If the wiki updates 5 minutes after the session, the next session might start before the update lands. Is that acceptable? Can the agent compensate by re-reading recent session transcripts directly?

3. **Provenance depth.** The evidence trail we built is agent-maintained (unreliable). Git gives diffs but not "this line came from turn X in session Y". A system with per-fact source tracking (the cited-fragments design we discussed) would be the real solution, but it's a significant data model change.

4. **Does the agent even need to write knowledge files, or just activity logs?** If the external system handles knowledge synthesis, the agent's only write responsibility is the activity log (what it delivered). That's the most mechanical part and the one most amenable to C-style extraction. The agent becomes read-only on knowledge, write-only on activity — simpler, more reliable.

5. **Cross-agent knowledge sharing.** Multiple agents (news-feed, discord-watch) would benefit from shared knowledge (user voice preferences, known sources). Files require manual symlinks or copies. A shared bank handles it automatically. Not tested yet.
