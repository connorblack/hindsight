/**
 * Client-side consumer for the reflect/recall SSE streams (see the dataplane's
 * _sse_operation_stream). Parses `progress` / `result` / `error` / `done` frames from
 * the fetch ReadableStream and dispatches them to typed handlers.
 */

export interface ProgressEvent {
  operation: "reflect" | "recall";
  operation_id: string;
  seq: number;
  phase: string;
  status: string;
  ts: string;
  message: string;
  // Typed per-phase payload (discriminated by `kind`); shape mirrors engine/streaming.py.
  data?: Record<string, unknown> | null;
}

export interface StreamHandlers<TResult> {
  onProgress?: (event: ProgressEvent) => void;
  onResult?: (result: TResult) => void;
  onError?: (message: string) => void;
}

/**
 * POST `body` to `url` and drive `handlers` from the resulting SSE stream. Resolves when
 * the stream ends; rejects only on a network-level failure (per-operation errors arrive
 * as an `error` frame via `handlers.onError`).
 */
export async function streamOperation<TResult>(
  url: string,
  body: unknown,
  handlers: StreamHandlers<TResult>,
  signal?: AbortSignal
): Promise<void> {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });

  if (!response.ok || !response.body) {
    handlers.onError?.(`Stream request failed (${response.status})`);
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  const dispatch = (frame: string) => {
    let eventName = "message";
    let data = "";
    for (const line of frame.split("\n")) {
      if (line.startsWith("event: ")) eventName = line.slice("event: ".length);
      else if (line.startsWith("data: ")) data += line.slice("data: ".length);
    }
    if (!data) return;
    if (eventName === "progress") handlers.onProgress?.(JSON.parse(data) as ProgressEvent);
    else if (eventName === "result") handlers.onResult?.(JSON.parse(data) as TResult);
    else if (eventName === "error") handlers.onError?.((JSON.parse(data) as ProgressEvent).message);
    // "done" carries no payload — the stream simply ends.
  };

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // SSE frames are separated by a blank line.
    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      if (frame.trim()) dispatch(frame);
    }
  }
}
