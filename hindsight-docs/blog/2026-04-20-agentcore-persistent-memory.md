---
title: "Your AgentCore Runtime Agent Forgets Everything When the Session Ends. Here's How to Fix That."
authors: [benfrank241]
date: 2026-04-20
tags: [agentcore, aws, bedrock, agents, memory, python]
description: "AgentCore Runtime sessions are ephemeral by design. hindsight-agentcore adds cross-session memory keyed to stable user identity, not transient runtime session IDs."
image: /img/blog/agentcore-persistent-memory.png
hide_table_of_contents: true
---

![Your AgentCore Runtime Agent Forgets Everything When the Session Ends. Here's How to Fix That.](/img/blog/agentcore-persistent-memory.png)

Amazon Bedrock AgentCore Runtime sessions are explicitly ephemeral — they terminate on inactivity and reprovision a fresh environment every time. The `hindsight-agentcore` package adds durable cross-session memory so your agents remember users, decisions, and learned patterns across any number of Runtime sessions.

<!-- truncate -->

## TL;DR

- AgentCore Runtime sessions are ephemeral by design — each invocation can start in a fresh environment
- `hindsight-agentcore` adds `before_turn()` and `after_turn()` hooks that automatically recall and retain memory around each agent execution
- Memory is keyed to **stable user identity** — never to the ephemeral `runtimeSessionId` — so it survives session churn
- `run_turn()` handles the full recall → execute → retain lifecycle in one call
- Failures are silent — if Hindsight is unavailable, your agent keeps running normally

---

## The Problem

AgentCore Runtime gives you a managed environment for running long-lived, agentic workloads on AWS. Tasks can span hours or days, sessions terminate on inactivity, and environments reprovision between invocations. This is the right architecture for durable agent workflows at scale.

The problem: **there's no built-in memory layer**.

Each Runtime session starts cold. An agent that learned a customer prefers email escalations over phone calls has no way to carry that preference to the next invocation. A background job that ran three analysis steps on Tuesday won't know it did that on Friday. Decisions, patterns, and hard-won context disappear the moment a session ends.

For demo agents this doesn't matter. For production agents handling real customer interactions, multi-step workflows, and evolving knowledge bases, it's a fundamental gap.

---

## The Approach

[Hindsight](https://github.com/vectorize-io/hindsight) is a memory layer for AI agents. It stores what agents learn, retrieves semantically relevant facts at query time, and returns formatted context ready to inject into a prompt.

The `hindsight-agentcore` package bridges AgentCore Runtime's invocation model to Hindsight's memory banks:

```
AgentCore Runtime invocation
        │
        ▼
   before_turn()         ← Recall relevant memories from Hindsight
        │
        ▼
  Agent executes          ← Prompt enriched with prior context
        │
        ▼
   after_turn()          ← Retain output to Hindsight (async, no latency cost)
```

Memory is keyed to **stable user identity** — not the `runtimeSessionId`. This is the critical design decision. Session IDs are ephemeral; they terminate when the session times out. User IDs are stable. Memory must survive session churn, which means it must be anchored to something that doesn't change when the environment reprovisions.

Default bank format:
```
tenant:{tenant_id}:user:{user_id}:agent:{agent_name}
```

---

## Implementation

### Install

```bash
pip install hindsight-agentcore
```

Python 3.10+ required.

You'll also need a running Hindsight instance.

**Option 1 — Hindsight Cloud (no setup required)**

Sign up at [ui.hindsight.vectorize.io](https://ui.hindsight.vectorize.io/signup) and grab your API URL and key from the dashboard.

**Option 2 — Self-hosted with Docker**

```bash
export OPENAI_API_KEY=sk-...

docker run --rm -it --pull always \
  -p 8888:8888 -p 9999:9999 \
  -e HINDSIGHT_API_LLM_API_KEY=$OPENAI_API_KEY \
  -e HINDSIGHT_API_LLM_MODEL=gpt-4o-mini \
  -v $HOME/.hindsight-docker:/home/hindsight/.pg0 \
  ghcr.io/vectorize-io/hindsight:latest
```

The API listens on port `8888`. Hindsight also supports Anthropic, Gemini, Groq, and Ollama — swap `HINDSIGHT_API_LLM_PROVIDER` and the key accordingly.

### Basic Setup

```python
import os
from hindsight_agentcore import HindsightRuntimeAdapter, TurnContext, configure

configure(
    hindsight_api_url="https://api.hindsight.vectorize.io",  # or your self-hosted URL
    api_key=os.environ["HINDSIGHT_API_KEY"],
)

adapter = HindsightRuntimeAdapter(agent_name="support-agent")
```

`configure()` sets global defaults. `HindsightRuntimeAdapter` is the main class — create one at module load time and reuse it across invocations.

### The TurnContext

Every memory operation is anchored to a `TurnContext` that captures the stable identity fields from the Runtime invocation:

```python
context = TurnContext(
    runtime_session_id=event["sessionId"],      # ephemeral — changes every invocation
    user_id=jwt_claims["sub"],                  # stable — from validated token
    agent_name="support-agent",
    tenant_id=jwt_claims.get("tenant"),
    request_id=event.get("requestId"),
)
```

The `runtime_session_id` is recorded for traceability but **never used as the bank key**. The bank is derived from `tenant_id`, `user_id`, and `agent_name` — fields that don't change when a session expires.

### High-Level Wrapper: `run_turn()`

For most use cases, `run_turn()` is all you need:

```python
async def handler(event: dict) -> dict:
    context = TurnContext(
        runtime_session_id=event["sessionId"],
        user_id=event["agentCoreContext"]["userId"],
        agent_name="support-agent",
        tenant_id=event["agentCoreContext"].get("tenantId"),
        request_id=event.get("requestId"),
    )

    result = await adapter.run_turn(
        context=context,
        payload={"prompt": event["prompt"]},
        agent_callable=run_my_agent,
    )
    return result


async def run_my_agent(payload: dict, memory_context: str) -> dict:
    prompt = payload["prompt"]
    if memory_context:
        prompt = f"Past context:\n{memory_context}\n\nCurrent request: {prompt}"

    output = await call_bedrock(prompt)
    return {"output": output}
```

`run_turn()` calls `before_turn()` to recall relevant memories, passes them to your `agent_callable` as a formatted string, then calls `after_turn()` to retain the output. The retention fires as a background task — the invocation returns before the write completes.

### Lower-Level Hooks

If you need more control — custom retrieval logic, conditional retention, or separate recall/retain steps — use the hooks directly:

```python
async def handler(event: dict) -> dict:
    context = TurnContext(...)
    user_message = event["prompt"]

    # Step 1: recall before executing
    memory_context = await adapter.before_turn(context, query=user_message)

    # Step 2: your agent runs with recalled context
    result = await run_my_agent(event, memory_context=memory_context)

    # Step 3: retain after executing (fires async by default)
    await adapter.after_turn(
        context,
        result=result["output"],
        query=user_message,
    )

    return result
```

### Retrieval Modes

**Recall (default)** — fast multi-strategy retrieval using semantic search, BM25, graph traversal, and temporal scoring in parallel:

```python
from hindsight_agentcore import RecallPolicy

adapter = HindsightRuntimeAdapter(
    agent_name="support-agent",
    recall_policy=RecallPolicy(mode="recall", budget="mid", max_tokens=1500),
)
```

**Reflect** — LLM-synthesized context for complex reasoning tasks. Use this when you need a coherent narrative from memory rather than a list of facts:

```python
adapter = HindsightRuntimeAdapter(
    agent_name="support-agent",
    recall_policy=RecallPolicy(mode="reflect"),
)
```

Reflect is slower and uses more tokens. Reserve it for planning steps or routing decisions where quality matters more than latency.

---

## Identity and Auth

**Never use `runtimeSessionId` as the bank ID or user identity.** This bears repeating because it's the most common mistake when adding memory to session-based systems.

Sessions expire. `runtimeSessionId` values are garbage-collected. If you key memory to a session ID, every new invocation gets a blank slate — which is exactly the problem you're trying to solve.

Preferred identity sources for `user_id`, in order of reliability:

1. JWT `sub` claim from a validated AgentCore OAuth token
2. `X-Amzn-Bedrock-AgentCore-Runtime-User-Id` header (set by the Runtime from auth context)
3. Application-supplied user ID passed via a trusted server-side mechanism

```python
# Good — stable identity from validated token
context = TurnContext(
    runtime_session_id=event["sessionId"],
    user_id=jwt_claims["sub"],          # doesn't change when session expires
    agent_name="support-agent",
    tenant_id=jwt_claims.get("tenant"),
)
```

The bank resolver enforces this by failing closed (`BankResolutionError`) if identity is missing, rather than defaulting to some insecure fallback. No identity → no memory operation. This prevents cross-user memory leakage in misconfigured deployments.

---

## Long-Running Workflows

For background jobs that span multiple Runtime sessions, retain checkpoints at the start and completion of each stage:

```python
# Job starts — record the intent
await adapter.after_turn(
    context,
    result=f"Starting QBR report generation for {company_name}",
    query=task_description,
)

# ... long-running analysis across potentially multiple sessions ...

# Job completes — record the outcome
await adapter.after_turn(
    context,
    result=f"QBR analysis complete. Top risks: {summary}",
    query=task_description,
)
```

Next time the agent is invoked for this user, `before_turn()` will surface the prior task history — what was started, what was found, what still needs doing.

---

## Async Retention

By default, `after_turn()` fires retention as a background task using `asyncio.ensure_future()`. The invocation returns before the memory write completes:

```python
configure(retain_async=True)   # default — non-blocking
configure(retain_async=False)  # await retention before returning
```

In AWS Lambda functions where the process may exit immediately after returning, set `retain_async=False` to ensure the write completes before the invocation ends.

---

## Failure Modes

| Failure | Behavior |
|---|---|
| Hindsight unavailable | `before_turn()` returns `""`, agent continues normally |
| Recall timeout | Returns `""`, agent continues normally |
| Retain failure | Logged as warning, invocation unaffected |
| Missing user identity | `BankResolutionError` — memory operation skipped |

Memory is enhancement, not infrastructure. The adapter is designed so that any Hindsight failure — network error, timeout, partial outage — results in the agent running without memory context, not in a failed invocation.

---

## Pitfalls and Edge Cases

**Don't key memory to the session ID.**
Already covered, but it bears repeating: `runtimeSessionId` is ephemeral. `user_id` is stable. Memory must be anchored to something that survives session churn.

**Lambda process lifecycle and async retention.**
In Lambda-style environments that terminate immediately after the handler returns, background tasks may not complete. Set `retain_async=False` if you need the write to commit before the process exits.

**`before_turn()` returns an empty string when there's nothing relevant.**
Check before injecting into your prompt:
```python
if memory_context:
    prompt = f"Past context:\n{memory_context}\n\n{user_message}"
```
Injecting an empty block into the system prompt wastes tokens and can confuse some models.

**Thread safety.**
`HindsightRuntimeAdapter` creates a thread-local Hindsight client. This is safe for concurrent async workloads where multiple coroutines share the adapter, and for thread-pool environments where each thread gets its own client.

---

## Tradeoffs and Alternatives

**When not to use this:**
If your AgentCore agents are truly stateless by design — independent tasks with no meaningful continuity between invocations — memory adds latency and complexity for no benefit.

**In-session vs. cross-session memory:**
AgentCore Runtime manages within-session context via its own session state mechanism. `hindsight-agentcore` is for cross-session memory — facts that need to survive beyond a single Runtime session. Don't use it as a substitute for in-session state.

**Alternatives:**
- **Amazon Bedrock AgentCore Memory**: AWS's built-in managed memory service, designed specifically for AgentCore agents. Tighter integration, less configuration — the right choice if you want zero external dependencies and are fully committed to the AgentCore Runtime model.
- **DynamoDB / ElastiCache**: Custom persistence with full control. More code, no semantic retrieval — you'll need to build your own relevance layer if you want anything beyond exact-match lookup.
- **Self-hosted Hindsight on ECS/EKS**: For AWS deployments where data must stay in your account, run Hindsight on ECS or EKS backed by RDS PostgreSQL with pgvector. The network path stays entirely within your VPC.

---

## Recap

AgentCore Runtime sessions are ephemeral by design. `hindsight-agentcore` adds a durable memory layer so agents remember across invocations — the three things you need: recall relevant context before the agent runs, retain output after it finishes, and key everything to stable user identity rather than the ephemeral session ID.

The mental model: sessions are transient, users are not. Memory belongs to the user, not the session.

---

## Next Steps

- **Hindsight Cloud:** Create an account at [ui.hindsight.vectorize.io/signup](https://ui.hindsight.vectorize.io/signup)
- **Self-hosting:** Start a server with the [developer quickstart](/developer/api/quickstart)
- **Package:** Review [`hindsight-agentcore` on PyPI](https://pypi.org/project/hindsight-agentcore/)
- **API docs:** Read the [Recall API](/developer/api/recall) and [Retain API](/developer/api/retain)
- **Related posts:** Compare the patterns in [OpenAI Agents persistent memory](/blog/2026/04/17/openai-agents-persistent-memory) and [Strands persistent memory](/blog/2026/03/28/strands-persistent-memory)
