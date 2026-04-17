---
name: agent-knowledge
description: Manage your long-term knowledge. Read existing topic pages before acting. Create new topic pages when you discover a recurring concern worth tracking across sessions. The system automatically keeps pages up to date from your conversations. Use when starting a task, when the user asks "what do you know about X", or when you realize a topic deserves its own persistent page.
---

# Agent Knowledge

Your knowledge is stored in Hindsight as topic pages (mental models) inside a Knowledge Base (KB). The system keeps pages updated automatically from your conversations. You **read** pages and **create** new ones when needed. You never edit page content directly — the system handles that.

**Bank:** `nicolo-news-feed`
**KB:** `news-feed`
**CLI:** `/Users/nicoloboschi/dev/hindsight-wt3/hindsight-cli/target/release/hindsight`
**API:** `http://localhost:8888`

## Mandatory startup sequence

Run these silently at the start of every session:

```bash
# 1. Ensure the KB exists (no-op if already created)
curl -sS "http://localhost:8888/v1/default/banks/nicolo-news-feed/knowledge-bases" | grep -q '"news-feed"' || \
  curl -sS -X POST "http://localhost:8888/v1/default/banks/nicolo-news-feed/knowledge-bases" \
    -H "Content-Type: application/json" \
    -d '{"id":"news-feed","name":"News Feed KB","mission":"User preferences and procedures for the news feed agent","auto_create":false}'

# 2. List your topic pages
curl -sS "http://localhost:8888/v1/default/banks/nicolo-news-feed/mental-models?kb=news-feed&detail=content"
```

Read the pages relevant to the current task. If the list is empty, that's fine — create pages as you learn things (see below).

## Reading pages

```bash
# List all pages (names + content)
curl -sS "http://localhost:8888/v1/default/banks/nicolo-news-feed/mental-models?kb=news-feed&detail=content"

# Read one specific page
/Users/nicoloboschi/dev/hindsight-wt3/hindsight-cli/target/release/hindsight mental-model get nicolo-news-feed <page_id> --output json

# Search across all knowledge
/Users/nicoloboschi/dev/hindsight-wt3/hindsight-cli/target/release/hindsight memory recall nicolo-news-feed "<query>" --output json
```

## Creating pages

When you discover a recurring topic worth tracking across sessions — user preferences, a procedure that works, a source list — create a page for it. Use your judgment, same as you would with a local file.

```bash
/Users/nicoloboschi/dev/hindsight-wt3/hindsight-cli/target/release/hindsight mental-model create nicolo-news-feed \
  "<Page Name>" \
  "<source_query: a question that, when answered from observations, produces the page content>" \
  --id <page-id> \
  --trigger-refresh-after-consolidation
```

**The `source_query` is the key field.** It's a question the system will re-ask on every consolidation to rebuild the page content from your accumulated observations. Write it as a comprehensive question about what the user wants.

Example:
```bash
/Users/nicoloboschi/dev/hindsight-wt3/hindsight-cli/target/release/hindsight mental-model create nicolo-news-feed \
  "Feed Source Preferences" \
  "What RSS feeds, websites, and sources does the user want included or excluded from their AI news feed, and in what priority order?" \
  --id feed-sources \
  --trigger-refresh-after-consolidation
```

**When to create a page:**
- You've seen the same topic come up 2-3 times across turns
- The user stated a durable preference or rule you'll need next session
- You discovered a procedure that works and want to remember it

**When NOT to create a page:**
- One-off facts (just acknowledge and move on — the system retains the conversation)
- Things that are already covered by an existing page
- Agent internals, tool usage, or delivered content

## How pages stay current

1. Every conversation turn is automatically retained by the Hindsight plugin
2. The system extracts observations from your conversations
3. After consolidation, pages with `refresh_after_consolidation` re-run their source_query against the latest observations
4. Next time you read the page, the content reflects the latest user feedback

You don't need to update pages manually. Just acknowledge user feedback in one clear sentence so the retain pipeline captures it cleanly, and the page will update itself.

## Rules

- **Never edit page content directly** — the system synthesizes it from observations
- **Never ask the user about knowledge structure** — which pages exist, naming, organization. That's your decision, invisible to the user.
- **Create pages silently** — don't announce "I'm creating a page for X". Just do it.
- **Prefer fewer broader pages** — one "preferences" page is better than three narrow ones
