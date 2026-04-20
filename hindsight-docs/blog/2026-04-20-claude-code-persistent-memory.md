---
title: "How to Add Persistent Memory to Claude Code with Hindsight"
authors: [benfrank241]
date: 2026-04-20
tags: [claude-code, anthropic, memory, persistent-memory, hindsight, tutorial, coding-agents]
description: "Give Claude Code persistent memory across sessions with Hindsight. Auto-recall injects relevant context before each prompt, and auto-retain turns past conversations into reusable memory."
image: /img/blog/claude-code-persistent-memory.png
hide_table_of_contents: true
---

![How to Add Persistent Memory to Claude Code with Hindsight](/img/blog/claude-code-persistent-memory.png)

Claude Code is excellent at working with the code in front of it, but it does not build durable semantic memory from your past sessions by default. `hindsight-memory` adds that missing layer by recalling relevant context before each prompt and retaining useful facts after the session ends.

<!-- truncate -->

## TL;DR

- Claude Code is excellent at working with your current codebase, but it does not build semantic long-term memory from your past conversations by default.
- Hindsight adds persistent memory through Claude Code's hook system: recall runs before prompts, retain runs after responses.
- The setup is small. Install one Claude Code plugin, choose local or external Hindsight, and start Claude Code normally.
- Use static memory like `CLAUDE.md` for durable rules, and Hindsight for the context that emerges while you work: decisions, preferences, bugs, and project history.
- The main caveats are startup/config friction, recall latency, and choosing the right bank strategy for single-project versus team memory.

## The Problem: Claude Code Remembers Files Better Than Conversations

[Claude Code](https://code.claude.com/docs/en/overview) already has useful memory primitives. It reads project instructions from `CLAUDE.md`, supports rules and skills in the `.claude` directory, and keeps the current session coherent while it explores your repo.

That solves the static part of memory well.

What it does not do by default is turn your past conversations into a reusable, semantic memory layer. If you told Claude Code last week why the auth middleware is fragile, why you stopped using a certain package, or which integration is blocked on a customer decision, that context is not automatically extracted and carried forward as structured long-term memory.

So the pattern becomes familiar:

- you restate architectural decisions
- you re-explain project conventions that were never written down
- you rediscover bugs the agent already helped you debug in a prior session
- you keep `CLAUDE.md` cleaner than a transcript, but much poorer than what actually happened

That is the gap Hindsight fills.

## The Approach: Add Recall and Retain Around Claude Code Hooks

Hindsight integrates with Claude Code as a plugin and uses Claude Code's hook lifecycle rather than bolting on a separate chat wrapper.

The model is simple:

```text
User prompt
  -> Claude Code UserPromptSubmit hook
  -> Hindsight recall
  -> relevant memories injected as additionalContext
  -> Claude sees the context and responds
  -> Claude Code Stop hook
  -> Hindsight retain extracts facts from the transcript
  -> memories land in the bank for future sessions
```

According to the Hindsight Claude Code integration docs, the plugin uses four Claude Code hook events:

- `SessionStart` for health checks
- `UserPromptSubmit` for recall
- `Stop` for retention
- `SessionEnd` for cleanup

The important design choice is where the recalled memory goes. Hindsight formats recalled items into a `<hindsight_memories>` block and passes it as `additionalContext`. Claude sees it, but it does not clutter the visible chat transcript.

That makes the workflow feel native. You keep using Claude Code the way you already do. The memory layer works before and after the model turn.

## Install the Hindsight Plugin

The documented quick start is straightforward:

```bash
claude plugin marketplace add vectorize-io/hindsight
claude plugin install hindsight-memory
```

The Claude Code CLI supports both commands directly:

- `claude plugin marketplace add <source>`
- `claude plugin install <plugin>`

After install, start Claude Code normally:

```bash
claude
```

The plugin activates automatically.

> **Note:** This guide focuses on Claude Code in the terminal. The same plugin also works with Claude Code Channels, but the pairing and access model deserve a separate setup guide.

## Choose a Connection Mode

Hindsight supports three useful modes for Claude Code. Pick based on where you want extraction to happen and how much operational overhead you want.

### 1. External Hindsight API

This is the cleanest setup for teams and the least operationally surprising once you are past initial signup.

```json
{
  "hindsightApiUrl": "https://your-hindsight-server.com",
  "hindsightApiToken": "your-token"
}
```

In this mode, Claude Code talks to an existing Hindsight server, cloud or self-hosted. The server handles fact extraction, so you do not need to manage a local extraction model in Claude Code.

If your goal is shared team memory, centralized ops, or memory that follows you across machines, this is the mode to lead with.

### 2. Auto-managed local daemon

If you want everything local, the plugin can manage `hindsight-embed` for you.

```json
{
  "hindsightApiUrl": "",
  "apiPort": 9077
}
```

For local extraction, you also need an LLM provider. The docs show a few options:

```bash
export OPENAI_API_KEY="sk-your-key"
# or
export ANTHROPIC_API_KEY="your-key"
# or, for personal/local use only
export HINDSIGHT_LLM_PROVIDER=claude-code
```

This mode is attractive for personal workflows because it keeps the architecture small. But it does add moving parts: `uvx`, daemon lifecycle, local logs, and local provider configuration.

### 3. Existing local Hindsight server

If you already run `hindsight-embed` yourself, point the plugin at that port and let Claude Code reuse it.

This is a good fit when Claude Code is only one client of a broader local Hindsight setup.

## A Minimal Config That Is Good Enough to Start

The defaults are sensible. You do not need to tweak everything up front.

A practical first config looks like this:

```json
{
  "bankId": "claude_code",
  "bankMission": "You are a Claude Code AI assistant. Focus on technical discussions, decisions, and context relevant to the user's projects.",
  "retainMission": "Extract technical decisions, architectural choices, user preferences, project context, and people/tool relationships. Ignore routine greetings and transient operational details.",
  "recallBudget": "mid",
  "recallMaxTokens": 1024,
  "autoRecall": true,
  "autoRetain": true
}
```

That gives you the important parts immediately:

- one named memory bank
- a clear identity for recall
- a focused extraction policy for retention
- balanced recall depth
- both recall and retain enabled

Resist the urge to over-engineer this on day one. The biggest win is turning memory on at all.

## What Gets Remembered

Hindsight is most useful when it captures the layer of engineering context that does not belong in the repo itself.

Examples:

- architecture decisions and why they were made
- coding conventions that came up during real work, not just in a style guide
- known bugs and workarounds
- project-specific constraints
- preferences about tools, testing, or deployment workflows
- relationships between people, tools, repos, and ongoing work

That is materially different from a static project instruction file.

A `CLAUDE.md` file is still the right place for things like:

- build and test commands
- repo layout
- stable project conventions
- permission boundaries
- team norms you want loaded every session

Hindsight complements that with the dynamic layer, what was learned while the work was happening.

## Per-Project Memory Is Usually the First Upgrade

One shared bank for everything is fine when you are just kicking the tires. It gets messy once you work across multiple repos.

The Claude Code integration supports dynamic bank IDs. The documented default granularity is `agent` plus `project`, which is exactly what most solo developers want.

```json
{
  "dynamicBankId": true,
  "dynamicBankGranularity": ["agent", "project"]
}
```

That means the memory you build while working in one repo does not bleed into another unrelated project.

This matters more than people expect. Without project isolation, recall quality degrades because the bank contains true facts that are correct, just not correct for this repo.

For most solo developers, I would enable this early, especially if you bounce between multiple client repos or side projects.

## Shared Team Memory Is the Second Upgrade

Once the single-user case is working, the more interesting pattern is team memory.

Point multiple Claude Code installs at the same external Hindsight endpoint with the same `bankId`, and now one developer's discoveries become available to everyone else's agent.

That changes the value proposition from "my assistant remembers me" to "our agents remember the team."

It is especially useful for:

- architecture rationale
- operational gotchas
- migration status
- integration edge cases
- historical context that never made it into docs

If that is your use case, use an external API and a deliberate `retainMission`. Shared memory without a tight retention policy turns into collective junk very quickly.

## What This Looks Like in Practice

Imagine a normal Claude Code workflow.

On Monday, you spend an hour debugging why a billing retry job is producing duplicates. You discover that the queue consumer is safe only when the idempotency key is derived from the provider event ID, not the internal retry ID. You talk through the fix with Claude Code while editing and testing.

Without Hindsight, that knowledge lives in your terminal scrollback and maybe in your head.

With Hindsight, the retain hook processes the transcript, extracts the key fact, and stores it in the bank.

On Thursday, you open Claude Code and ask it to extend the billing pipeline. The recall hook runs before the model turn, pulls back the prior context about idempotency and retries, and injects it into Claude's context. You did not rewrite the backstory. Claude did not start from zero.

That is the real win here. Not generic "AI memory," but less re-explanation and fewer repeated mistakes.

## Pitfalls and Edge Cases

This setup is conceptually simple, but there are a few failure modes worth calling out.

### 1. No memories appear in the first session

This is normal.

Recall depends on prior retained material. If the bank is empty, there is nothing to retrieve yet. The first useful recall usually shows up after you have finished at least one real session.

### 2. The local daemon path has more moving parts

If you choose local mode, you now depend on:

- `uvx` availability
- a working local LLM provider configuration
- daemon startup and health checks
- local logs when something goes wrong

That is not a reason to avoid local mode. It is a reason to present external API mode as the simpler production recommendation.

### 3. Recall quality falls off when the bank is too broad

A bank that mixes unrelated repos, agents, channels, or users will still return true memories, but not necessarily the right ones.

Use `dynamicBankId` for isolation, or set a deliberate shared `bankId` only when you actually want one shared brain.

### 4. Recall adds latency

The docs are explicit that recall budget affects speed. `low` is faster, `high` is more thorough, `mid` is the default balance.

If developers are extremely latency-sensitive, lead with:

```json
{
  "recallBudget": "low",
  "recallMaxTokens": 512
}
```

Then raise it only if recall quality is too thin.

### 5. Bad retention instructions create bad memory

If your `retainMission` is vague, the bank fills with trivia. If it is too narrow, you miss useful context.

A good retention mission is opinionated. Tell Hindsight exactly what should count as durable memory and what should be ignored.

### 6. Static memory and dynamic memory are not substitutes

If you use Hindsight as an excuse not to maintain `CLAUDE.md`, the result is worse, not better.

Keep stable instructions in `CLAUDE.md`. Let Hindsight capture what emerges during work.

## Tradeoffs and Alternatives

There are three realistic alternatives here.

### Alternative 1: Just use `CLAUDE.md` and built-in project memory

This is the lowest-friction option and still worth doing.

Use it when:

- your project context is stable
- you only need explicit rules and commands
- you do not care about semantic recall from prior conversations

Do not use it as the whole story if you want the agent to accumulate working knowledge from real sessions.

### Alternative 2: Use Hindsight with a local daemon

Use this when:

- you want local control
- your workflow is mostly personal
- you are comfortable managing one more local service boundary

The tradeoff is operational complexity.

### Alternative 3: Use Hindsight with an external API

Use this when:

- you want the simplest day-two operations
- you want memory across machines
- you want shared team memory
- you do not want each Claude Code client doing its own extraction setup

The tradeoff is that memory is no longer purely local.

My default recommendation is simple: start with external API if you care about reliability or teams, local daemon if you care most about locality and personal workflows.

## Recap

Claude Code already has strong project context mechanisms, but they are mostly static. Hindsight adds the missing dynamic layer.

The integration works by using Claude Code hooks to do two things automatically:

- recall relevant memories before each prompt
- retain useful facts after responses

That gives you a workflow where Claude Code can remember the decisions, bugs, and preferences that emerged in prior sessions, without stuffing your visible chat transcript with old context.

Used well, the mental model is straightforward:

- `CLAUDE.md` is for rules
- Hindsight is for learned context
- dynamic banks keep recall clean
- shared banks turn personal agent memory into team memory

## Next Steps

- **Hindsight Cloud:** Create an account at [ui.hindsight.vectorize.io/signup](https://ui.hindsight.vectorize.io/signup)
- **Integration docs:** Read the [Claude Code integration guide](/sdks/integrations/claude-code)
- **Self-hosting:** Start a local server with the [developer quickstart](/developer/api/quickstart)
- **Related post:** See [Adding Persistent Memory to OpenAI Codex with Hindsight](/blog/2026/04/08/adding-memory-to-codex-with-hindsight)
- **Team setup:** Use a shared bank with [Shared Memory for AI Coding Agents](/blog/2026/03/31/team-shared-memory-ai-coding-agents)
