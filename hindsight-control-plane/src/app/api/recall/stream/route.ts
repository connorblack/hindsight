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
    types,
    fact_type,
    prefer_observations,
    max_tokens,
    trace,
    budget,
    include,
    query_timestamp,
    tags,
    tags_match,
    min_scores,
  } = body;

  // Mirror the non-streaming /api/recall body shaping.
  const requestBody = {
    query,
    types: types || fact_type,
    prefer_observations,
    max_tokens,
    trace,
    budget: budget || "mid",
    include,
    query_timestamp,
    tags,
    tags_match,
    min_scores,
  };

  // Proxy the dataplane SSE stream straight through. Headers flush immediately, so this
  // never hits undici's default request timeout.
  const upstream = await fetch(dataplaneBankUrl(bankId, "/memories/recall/stream"), {
    method: "POST",
    headers: getDataplaneHeaders({
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    }),
    body: JSON.stringify(requestBody),
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
