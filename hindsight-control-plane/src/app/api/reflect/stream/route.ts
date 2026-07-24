import { NextRequest, NextResponse } from "next/server";
import { localizeApiErrorPayload } from "@/lib/i18n/api-errors";
import { dataplaneBankUrl, getDataplaneHeaders } from "@/lib/hindsight-client";

// Streaming proxy: needs the Node runtime (undici fetch streaming) and must never be
// statically optimized.
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function POST(request: NextRequest) {
  let body;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json(
      localizeApiErrorPayload(request, {
        error: "Invalid JSON body",
        errorKey: "api.errors.auth.invalidRequestBody",
      }),
      { status: 400 }
    );
  }

  const bankId = body.bank_id || body.agent_id || "default";
  const {
    query,
    budget,
    thinking_budget,
    include_facts,
    include_tool_calls,
    tags,
    tags_match,
    max_tokens,
    fact_types,
    exclude_mental_models,
    exclude_mental_model_ids,
  } = body;

  // Mirror the non-streaming /api/reflect body shaping so the two paths behave identically.
  const requestBody: Record<string, unknown> = {
    query,
    budget: budget || (thinking_budget ? "mid" : "low"),
    tags,
    tags_match,
    max_tokens: max_tokens || undefined,
    fact_types: fact_types || undefined,
    exclude_mental_models: exclude_mental_models || undefined,
    exclude_mental_model_ids: exclude_mental_model_ids || undefined,
  };
  const includeOptions: Record<string, unknown> = {};
  if (include_facts) includeOptions.facts = {};
  if (include_tool_calls) includeOptions.tool_calls = {};
  if (Object.keys(includeOptions).length > 0) requestBody.include = includeOptions;

  // Proxy the dataplane SSE stream straight through to the browser. Streaming flushes
  // response headers immediately, so this fetch never hits undici's 300s headersTimeout
  // — the cause of the old non-streaming 502s on long reflects.
  const upstream = await fetch(dataplaneBankUrl(bankId, "/reflect/stream"), {
    method: "POST",
    headers: getDataplaneHeaders({
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    }),
    body: JSON.stringify(requestBody),
    // duplex is required by undici when sending a request body with fetch streaming.
    // @ts-expect-error - `duplex` is not yet in the DOM fetch types
    duplex: "half",
  });

  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no",
    },
  });
}
