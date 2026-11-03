"""Model ids, endpoints and prices for the Meta Model API (Muse).

Sources (fetched 2026-11-03):
  * Model catalogue:  https://dev.meta.ai/docs/models
  * Quickstart/base:  https://dev.meta.ai/docs/quickstart          (base URL https://api.meta.ai/v1)
  * Chat completions: https://dev.meta.ai/docs/protocols/chat-completions
  * Pricing:          https://dev.meta.ai/docs/pricing-rate-limits
  * Voice realtime:   https://dev.meta.ai/docs/api-reference/voice/realtime
  * Voice schemas:    https://dev.meta.ai/docs/api-reference/voice/schemas

Note: the older Llama API host (api.llama.com) was retired; the Muse models are served from
api.meta.ai. The env var names used by this project (LLAMA_API_KEY / LLAMA_API_BASE_URL) are kept
for compatibility with the rest of the hackathon code; the docs call the key MODEL_API_KEY.
"""
from __future__ import annotations

# ---------------------------------------------------------------- endpoints
DEFAULT_BASE_URL = "https://api.meta.ai/v1"                 # https://dev.meta.ai/docs/quickstart
CHAT_COMPLETIONS_PATH = "/chat/completions"                 # https://dev.meta.ai/docs/protocols/chat-completions
MODELS_PATH = "/models"                                     # https://dev.meta.ai/docs/api-reference/models/list-models
REALTIME_ASR_URL = "wss://api.meta.ai/v1/asr/realtime"      # https://dev.meta.ai/docs/api-reference/voice/realtime

# ------------------------------------------------------------------- models
# https://dev.meta.ai/docs/models
MUSE_SPARK_1_3 = "muse-spark-1.3"                 # "Recommended for new work"
MUSE_SPARK_1_3_CONTRIBUTOR = "muse-spark-1.3-contributor"
MUSE_SPARK_1_2 = "muse-spark-1.2"
MUSE_SPARK_1_1 = "muse-spark-1.1"
MUSE_WONDER_1_0 = "muse-wonder-1.0"
MUSE_VOICE_TRANSCRIBE_1_0 = "muse-voice-transcribe-1.0"

DEFAULT_CHAT_MODEL = MUSE_SPARK_1_3
DEFAULT_TRANSCRIBE_MODEL = MUSE_VOICE_TRANSCRIBE_1_0

# ------------------------------------------------------------------ pricing
# USD per 1M tokens, Standard mode. https://dev.meta.ai/docs/pricing-rate-limits
# muse-spark-1.3 / 1.2 / 1.1: cached input $0.15, input $1.25, output $4.25
# muse-spark-*-contributor:   cached input $0.002, input $0.10, output $0.20
# muse-wonder-1.0:            cached input $0.20, input $4.00, output $20.00
# Muse Voice Transcribe:      $0.18 per hour of audio (metered in whole seconds, rounded down)
PRICES_USD_PER_M: dict[str, tuple[float, float]] = {   # model -> (input, output)
    MUSE_SPARK_1_3: (1.25, 4.25),
    MUSE_SPARK_1_2: (1.25, 4.25),
    MUSE_SPARK_1_1: (1.25, 4.25),
    MUSE_SPARK_1_3_CONTRIBUTOR: (0.10, 0.20),
    "muse-spark-1.2-contributor": (0.10, 0.20),
    MUSE_WONDER_1_0: (4.00, 20.00),
}
TRANSCRIBE_USD_PER_HOUR = 0.18


def price_for(model: str) -> tuple[float, float]:
    """(input, output) USD per 1M tokens; unknown models fall back to Muse Spark pricing."""
    return PRICES_USD_PER_M.get(model, PRICES_USD_PER_M[MUSE_SPARK_1_3])
