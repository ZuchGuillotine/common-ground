"""Clients for Meta's Muse models: chat (Muse Spark) and streaming speech-to-text (Muse Voice Transcribe)."""
from .base import ChatClient, MuseError, TranscribeClient, TranscriptEvent, UsageMeter
from .chat import MuseChat, MuseChatError, extract_json
from .mock import MockMuseChat, MockMuseTranscribe
from .models import DEFAULT_CHAT_MODEL, DEFAULT_TRANSCRIBE_MODEL, MUSE_SPARK_1_3, MUSE_VOICE_TRANSCRIBE_1_0
from .transcribe import MuseTranscribe, MuseTranscribeError, TranscriptNormaliser

__all__ = [
    "ChatClient", "TranscribeClient", "TranscriptEvent", "UsageMeter", "MuseError",
    "MuseChat", "MuseChatError", "extract_json",
    "MuseTranscribe", "MuseTranscribeError", "TranscriptNormaliser",
    "MockMuseChat", "MockMuseTranscribe",
    "DEFAULT_CHAT_MODEL", "DEFAULT_TRANSCRIBE_MODEL", "MUSE_SPARK_1_3", "MUSE_VOICE_TRANSCRIBE_1_0",
]
