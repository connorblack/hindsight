"""Bridge the engine progress-streaming protocol onto MCP notifications.

Lets any MCP client (OpenWebUI/Hermes, an example FastMCP app) see reflect/recall
progress live. Each :class:`ProgressEvent` is sent as a structured **logging
notification** (``notifications/message``) whose ``extra`` carries the full
tool-use / response / stage payload — this needs no ``progressToken`` and is what
clients that render tool activity consume. Reflect iterations additionally drive a
numeric **progress notification** for clients that show a progress bar (a silent
no-op when the client sent no ``progressToken``).

MCP tool results are atomic (there is no partial-content primitive), so the final
answer is still returned whole by the tool; these notifications stream the steps
that led to it.
"""

import logging

from fastmcp import Context
from fastmcp.server.dependencies import get_context

from hindsight_api.engine.streaming import (
    ProgressEmitter,
    ProgressEvent,
    ProgressOperation,
)

logger = logging.getLogger(__name__)

# The full ProgressEvent is delivered nested under this single key in an MCP logging
# notification's ``extra``. FastMCP forwards ``extra`` into a Python ``LogRecord``,
# which raises if it contains reserved attribute names — and our event has a top-level
# ``message`` field (collides with ``LogRecord.message``). One wrapper key sidesteps
# every such collision. Downstream clients read ``extra["hindsight_event"]``.
MCP_EVENT_EXTRA_KEY = "hindsight_event"


class McpProgressEmitter(ProgressEmitter):
    """Delivers ProgressEvents to the active MCP client via ``ctx`` notifications."""

    def __init__(self, operation: ProgressOperation, operation_id: str, ctx: Context) -> None:
        super().__init__(operation, operation_id)
        self._ctx = ctx

    async def _deliver(self, event: ProgressEvent) -> None:
        # Push every event to BOTH MCP channels so the widest range of clients render
        # it; each is best-effort (a disconnected/non-conforming client must never break
        # the tool — progress is advisory), so each swallows its own errors.
        payload = event.model_dump(mode="json")

        # Logging (notifications/message): carries the full structured event in `extra`
        # — the rich tool-use/response/stage detail. Delivered only once the client has
        # set a logging level (logging/setLevel), which many clients do on connect.
        try:
            await self._ctx.info(event.message, extra={MCP_EVENT_EXTRA_KEY: payload})
        except Exception:
            logger.debug("MCP progress log notification failed (ignored)", exc_info=True)

        # Progress (notifications/progress): human-readable message only, but needs just
        # a progressToken (no logging level), so it reaches clients that render progress
        # even without enabling server logging. `seq` is the monotonic value MCP requires;
        # `total` is omitted because reflect's step count is not known up front.
        try:
            await self._ctx.report_progress(progress=event.seq, total=None, message=event.message)
        except Exception:
            logger.debug("MCP progress notification failed (ignored)", exc_info=True)


def mcp_progress_emitter(operation: ProgressOperation) -> McpProgressEmitter | None:
    """Emitter bound to the active MCP request context, or ``None`` outside one.

    ``get_context()`` raises ``RuntimeError`` when there is no active FastMCP request
    (e.g. the shared tool body invoked from a non-MCP path), in which case the caller
    simply skips streaming and the operation runs exactly as before.
    """
    try:
        ctx = get_context()
    except RuntimeError:
        return None
    if ctx is None:
        return None
    operation_id = getattr(ctx, "request_id", None) or "mcp"
    return McpProgressEmitter(operation, operation_id, ctx)
