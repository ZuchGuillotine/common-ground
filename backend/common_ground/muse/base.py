"""Interfaces shared by the real Muse clients and their mocks."""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, Self, runtime_checkable

TranscriptKind = Literal["partial", "final", "turn_end", "speaker_change"]
TranscribeMode = Literal["PUSH_TO_TALK", "ENDPOINTING", "DIARIZATION"]

Message = dict[str, Any]   # {"role": "system"|"user"|"assistant", "content": str}


@dataclass
class TranscriptEvent:
    """Normalised transcription event.

    kind:
      partial        revised (cumulative) text for the current turn; replaces the previous partial
      final          finished, post-processed text for a turn
      turn_end       the turn identified by turn_id is closed (always follows its final)
      speaker_change diarization label changed; text is empty
    speaker_label is "S0", "S1", ... (first-seen order) or None when unknown.
    t_start / t_end are seconds of audio since the start of the stream (None when unknown).
    raw is the untouched wire message that produced this event.
    """
    kind: TranscriptKind
    text: str = ""
    speaker_label: str | None = None
    t_start: float | None = None
    t_end: float | None = None
    turn_id: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class MuseError(RuntimeError):
    """Base error for the Muse clients."""


@dataclass
class UsageMeter:
    """Accumulates token usage and an estimated USD cost across calls."""
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    cost_usd: float = 0.0

    def add(self, input_tokens: int, output_tokens: int, price_per_m: tuple[float, float]) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.calls += 1
        self.cost_usd += (input_tokens * price_per_m[0] + output_tokens * price_per_m[1]) / 1_000_000

    def summary(self) -> str:
        return (f"{self.calls} call(s), {self.input_tokens} in / {self.output_tokens} out tokens, "
                f"~${self.cost_usd:.5f}")


@runtime_checkable
class ChatClient(Protocol):
    usage: UsageMeter

    async def complete(self, messages: list[Message], *, model: str | None = None,
                       json_schema: dict | None = None, max_tokens: int = 1024,
                       temperature: float | None = None) -> str: ...

    async def complete_json(self, messages: list[Message], *, model: str | None = None,
                            json_schema: dict | None = None, max_tokens: int = 1024,
                            temperature: float | None = None) -> dict: ...


@runtime_checkable
class TranscribeClient(Protocol):
    async def __aenter__(self) -> Self: ...
    async def __aexit__(self, *exc: object) -> None: ...
    async def send_pcm(self, pcm16: bytes) -> None: ...
    async def end_stream(self) -> None: ...
    def events(self) -> AsyncIterator[TranscriptEvent]: ...
