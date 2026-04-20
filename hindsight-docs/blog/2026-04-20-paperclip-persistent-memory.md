---
title: "Paperclip Agents Start Cold Every Heartbeat. Here's How to Fix That."
authors: [benfrank241]
date: 2026-04-20
tags: [paperclip, agents, memory, typescript, nodejs, multi-tenant]
description: "Paperclip agents wake up cold on every heartbeat. hindsight-paperclip adds recall before each task and retain after each run, with company and agent scoped memory."
image: /img/blog/paperclip-persistent-memory.png
hide_table_of_contents: true
---

![Paperclip Agents Start Cold Every Heartbeat. Here's How to Fix That.](/img/blog/paperclip-persistent-memory.png)

Paperclip AI agents are stateless by design. Every heartbeat wakes up cold — no memory of prior tasks, decisions, or patterns. The `hindsight-paperclip` package adds persistent long-term memory without changing how your agents work.

<!-- truncate -->

## TL;DR

- Paperclip agents start cold on every heartbeat — no memory of prior sessions
- `hindsight-paperclip` adds `recall()` before each task and `retain()` after, with no code changes to your agent logic
- `createMemoryMiddleware()` handles everything automatically for HTTP adapter agents
- Memory is isolated per company and agent by default — maps directly to Paperclip's multi-tenant model
- Failures are silent — if Hindsight is unavailable, your agent keeps running

---

## The Problem

Paperclip gives you a clean model for running autonomous AI agents inside multi-tenant organizational hierarchies. Agents check out tasks, execute using an underlying adapter (Claude, Codex, HTTP, Process), and report back. The orchestration layer is solid.

The problem: every heartbeat is a blank slate.

An agent that reviewed a PR last Tuesday has no idea it did that. An agent that learned your company uses Postgres for the main database won't know that next time. Decisions, preferences, and institutional knowledge disappear between runs. For demos this doesn't matter. For production agents managing real work across real companies, it's a fundamental gap.

---

## The Approach

[Hindsight](https://github.com/vectorize-io/hindsight) is a memory layer for AI agents. It stores what agents do, extracts semantically relevant facts at query time, and returns formatted context ready to inject into a prompt.

The `hindsight-paperclip` package maps Hindsight's memory model directly onto Paperclip's execution model:

```
Paperclip Heartbeat
        │
        ▼
   recall()              ← Query Hindsight for prior context
        │
        ▼
  Agent executes          ← Prompt enriched with memories
        │
        ▼
   retain()              ← Store output for future heartbeats
```

Memory is scoped by company and agent by default — `paperclip::{companyId}::{agentId}` — so Company A's agents never see Company B's memories, matching Paperclip's isolation model exactly.

---

## Implementation

### Install

```bash
npm install hindsight-paperclip
```

Node.js 20+ required. No runtime dependencies beyond `hindsight-paperclip` itself — uses native `fetch`.

You'll also need a running Hindsight instance.

**Option 1 — Hindsight Cloud (no setup required)**

Sign up at [ui.hindsight.vectorize.io](https://ui.hindsight.vectorize.io/signup) and grab your API URL and token from the dashboard.

**Option 2 — Self-hosted with Docker**

```bash
docker run -d -p 8888:8888 \
  -e HINDSIGHT_API_LLM_PROVIDER=openai \
  -e HINDSIGHT_API_LLM_API_KEY=sk-... \
  -e HINDSIGHT_API_LLM_MODEL=gpt-4o-mini \
  ghcr.io/vectorize-io/hindsight:latest
```

The API listens on port `8888`. Hindsight also supports Anthropic, Gemini, Groq, and Ollama.

### Process Adapter Agents

For agents running as scripts via Paperclip's Process adapter, call `recall()` and `retain()` directly:

```typescript
import { recall, retain, loadConfig } from 'hindsight-paperclip'

const config = loadConfig()  // reads HINDSIGHT_API_URL, HINDSIGHT_API_TOKEN

const companyId = process.env.PAPERCLIP_COMPANY_ID!
const agentId = process.env.PAPERCLIP_AGENT_ID!
const runId = process.env.PAPERCLIP_RUN_ID!

// Before executing — inject prior context into the agent prompt
const memories = await recall({
  companyId,
  agentId,
  query: process.env.TASK_DESCRIPTION ?? '',
}, config)

const systemPrompt = memories
  ? `Past context:\n${memories}\n\nCurrent task: ${process.env.TASK_DESCRIPTION}`
  : process.env.TASK_DESCRIPTION ?? ''

// ... agent executes ...

// After executing — store output for future heartbeats
await retain({
  companyId,
  agentId,
  content: agentOutput,
  documentId: runId,   // prevents duplicate storage if the run is retried
}, config)
```

`recall()` returns a formatted string ready to inject. `retain()` stores the output asynchronously. Both fail silently — memory is enhancement, not infrastructure.

### HTTP Adapter Agents

For agents running as Express webhook servers, use the middleware:

```typescript
import express from 'express'
import { createMemoryMiddleware, loadConfig } from 'hindsight-paperclip'
import type { HindsightRequest } from 'hindsight-paperclip'

const app = express()
app.use(express.json())
app.use(createMemoryMiddleware(loadConfig()))

app.post('/heartbeat', async (req, res) => {
  const { memories } = (req as HindsightRequest).hindsight
  const { context } = req.body

  const prompt = memories
    ? `Past context:\n${memories}\n\nCurrent task: ${context.taskDescription}`
    : `Task: ${context.taskDescription}`

  const output = await runYourAgent(prompt)
  res.json({ output })  // middleware auto-retains this
})
```

The middleware reads `agentId`, `companyId`, `runId`, and `context.taskDescription` from Paperclip's standard HTTP adapter request body. It recalls before the handler runs and retains the `output` field automatically after the response goes out — no boilerplate in your handler.

---

## What Memory Looks Like

After a few heartbeats, `recall()` returns something like:

```
- Fixed the login bug in auth.ts caused by a missing await on verifyToken(). [observation]

- User reviewed PR #42 and left comments on token expiry handling. [observation] (2026-03-31)

- Company A uses Postgres as the main database. [world]
```

Each line is a fact Hindsight extracted from prior agent outputs. The agent gets relevant context without the entire history — Hindsight handles the selection.

---

## Bank Isolation

By default, memory is scoped to the company + agent pair:

```
paperclip::{companyId}::{agentId}
```

You can change the granularity:

```typescript
// Shared memory across all agents in a company
loadConfig({ bankGranularity: ['company'] })
// → "paperclip::{companyId}"

// Agent's global memory across all companies
loadConfig({ bankGranularity: ['agent'] })
// → "paperclip::{agentId}"
```

This maps directly to Paperclip's organizational model. A company-level bank is useful for shared institutional knowledge. Agent-level banks work for agents that operate across multiple companies but need continuity.

---

## Configuration

| Option | Env Variable | Default | Description |
|---|---|---|---|
| `hindsightApiUrl` | `HINDSIGHT_API_URL` | Required | Hindsight server URL |
| `hindsightApiToken` | `HINDSIGHT_API_TOKEN` | — | API token for Hindsight Cloud |
| `bankGranularity` | — | `['company', 'agent']` | Memory isolation level |
| `recallBudget` | — | `'mid'` | Search depth: `low`, `mid`, `high` |
| `recallMaxTokens` | — | `1024` | Max tokens in recalled memory block |
| `retainContext` | — | `'paperclip'` | Provenance label stored with memories |
| `timeoutMs` | — | `15000` | Request timeout in milliseconds |

---

## Pitfalls & Edge Cases

**Silent failures are good for uptime, bad for debugging.** `hindsight-paperclip` is designed so your agent keeps running even if Hindsight is unavailable. That is the right default for production heartbeats, but it also means you need logs and metrics if you want to notice memory outages before someone asks why the agent stopped remembering.

**Bank granularity controls recall quality.** The default `paperclip::{companyId}::{agentId}` scope is usually the safest choice. If you collapse memory to just `company`, every agent in the org can see the same context. That is powerful for shared institutional knowledge, but it can also pollute recall with facts that are true for the company and irrelevant to this specific agent.

**Use stable `documentId` values on retries.** If a Paperclip run can be retried, pass the same `runId` into `documentId`. Otherwise you can retain near-duplicate outputs and slowly fill the bank with repeated memories.

**Recall adds one more network hop to every heartbeat.** Usually that is worth it. But if your agents are extremely latency-sensitive, keep the recall budget low and only retain the facts you actually want to surface later.

## Tradeoffs & Alternatives

**When not to use this:** If your Paperclip agents only handle one-off tasks with no continuity, plain task state may be enough. Persistent memory pays off when agents revisit the same codebases, companies, or workflows over time.

**Manual `recall()` / `retain()` vs middleware:** The Process adapter path gives you full control over what gets stored and what gets injected. The HTTP middleware path is simpler and usually the right default, but it is also more opinionated.

**One bank per company vs one bank per company+agent:** A company-level bank helps shared knowledge propagate across agents. A company+agent bank gives cleaner recall and fewer collisions. Most teams should start with the narrower scope, then broaden it only when they have a clear sharing need.

**Hindsight vs Paperclip task state:** Paperclip already gives you issues, heartbeats, and organizational structure. Hindsight is not a replacement for that. It adds semantic recall across runs, so the agent can reuse knowledge that never became an issue field or explicit task artifact.

## Recap

Paperclip's cold-start heartbeat model is a good fit for operational reliability, but it leaves every run without context from the last one. `hindsight-paperclip` fixes that by recalling relevant memories before the task starts and retaining useful outputs after it ends.

The key design choice is scope. Keep memory narrow enough that recall stays relevant, and broad enough that the right knowledge can compound over time.


## Next Steps

- **Hindsight Cloud:** Create an account at [ui.hindsight.vectorize.io/signup](https://ui.hindsight.vectorize.io/signup)
- **Self-hosting:** Start a server with the [developer quickstart](/developer/api/quickstart)
- **Paperclip integration:** Review the `hindsight-paperclip` package on [npm](https://www.npmjs.com/package/hindsight-paperclip)
- **API docs:** Read the [Recall API](/developer/api/recall) and [Retain API](/developer/api/retain)
- **Related pattern:** Compare with [Shared Memory for AI Coding Agents](/blog/2026/03/31/team-shared-memory-ai-coding-agents) when you want multiple agents sharing one bank deliberately
