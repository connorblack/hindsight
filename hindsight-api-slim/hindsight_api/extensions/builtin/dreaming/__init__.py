"""hindsight_dreaming — async "dreaming" dedup-reduce over committed observations.

A self-contained, ADDITIVE Hindsight extension (no core files modified). It reuses
the core consolidator's dedup machinery so a dream merge talks to the SAME
consolidation LLM with the SAME temporally-conservative prompt.

Load via environment variables:
    HINDSIGHT_API_HTTP_EXTENSION=hindsight_dreaming:DreamingHttpExtension
    HINDSIGHT_API_MCP_EXTENSION=hindsight_dreaming:DreamingMCPExtension
"""

from .config import DreamingConfig
from .dreaming import DreamingHttpExtension, DreamingMCPExtension

__all__ = [
    "DreamingHttpExtension",
    "DreamingMCPExtension",
    "DreamingConfig",
]
