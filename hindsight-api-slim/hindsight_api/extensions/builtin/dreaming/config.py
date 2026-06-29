"""Configuration for the Hindsight ``dreaming`` extension.

Read from ``HINDSIGHT_API_DREAM_*`` environment variables via ``os.getenv`` —
the *same* way ``hindsight_api.config.HindsightConfig.from_env`` reads its
``HINDSIGHT_API_*`` settings.

NOTE on the two config channels:
  * The extension loader (``hindsight_api.extensions.loader``) only collects env
    vars matching the *extension* prefix (``HINDSIGHT_API_HTTP_*`` for an HTTP
    extension) into the ``config`` dict handed to ``__init__``. Our tunables use
    the ``HINDSIGHT_API_DREAM_*`` namespace instead, so they are read directly
    from the environment here rather than from that dict. This keeps the dream
    knobs in their own clearly-named namespace and lets the SAME values feed both
    the HTTP and the MCP extension (each of which is loaded from a *different*
    loader prefix).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

ENV_PREFIX = "HINDSIGHT_API_DREAM_"

ENV_ENABLED = ENV_PREFIX + "ENABLED"
ENV_REDUCE_THRESHOLD = ENV_PREFIX + "REDUCE_THRESHOLD"
ENV_INTERVAL_SECONDS = ENV_PREFIX + "INTERVAL_SECONDS"
ENV_K = ENV_PREFIX + "K"
ENV_PARALLELISM = ENV_PREFIX + "PARALLELISM"
ENV_MAX_SCOPES = ENV_PREFIX + "MAX_SCOPES"

# Defaults mirror the approved plan (Part 2, Layer 2).
DEFAULT_ENABLED = True
DEFAULT_REDUCE_THRESHOLD = 0.94
DEFAULT_INTERVAL_SECONDS = 0  # 0 = background scheduler disabled
DEFAULT_K = 100
DEFAULT_PARALLELISM = 16
DEFAULT_MAX_SCOPES = 0  # 0 = unlimited


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y", "t"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class DreamingConfig:
    """Resolved dreaming tunables.

    ``reduce_threshold`` is the cosine-similarity floor for a k-NN candidate pair
    (``1 - cosine_distance >= reduce_threshold``). It is a *recall* gate only —
    the consolidation LLM (``_DEDUP_PROMPT``) is the precision gate that actually
    decides a merge.
    """

    enabled: bool = DEFAULT_ENABLED
    reduce_threshold: float = DEFAULT_REDUCE_THRESHOLD
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    k: int = DEFAULT_K
    parallelism: int = DEFAULT_PARALLELISM
    max_scopes: int = DEFAULT_MAX_SCOPES

    @classmethod
    def from_env(cls) -> "DreamingConfig":
        return cls(
            enabled=_env_bool(ENV_ENABLED, DEFAULT_ENABLED),
            reduce_threshold=_env_float(ENV_REDUCE_THRESHOLD, DEFAULT_REDUCE_THRESHOLD),
            interval_seconds=_env_int(ENV_INTERVAL_SECONDS, DEFAULT_INTERVAL_SECONDS),
            k=_env_int(ENV_K, DEFAULT_K),
            parallelism=_env_int(ENV_PARALLELISM, DEFAULT_PARALLELISM),
            max_scopes=_env_int(ENV_MAX_SCOPES, DEFAULT_MAX_SCOPES),
        )
