"""Offline stand-ins for MuseChat and MuseTranscribe (same interfaces, no network)."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any, Self

from .base import Message, TranscriptEvent, UsageMeter
from .chat import extract_json
from .models import DEFAULT_CHAT_MODEL, price_for

Responder = Callable[[list[Message], dict | None], Any]


class MockMuseChat:
    """`responses` is either a callable (messages, json_schema) -> dict|str, or a dict whose keys
    are matched as substrings of the prompt text (or the schema title). `default` is used when
    nothing matches. Every call is recorded in `calls`."""

    def __init__(self, responses: dict[str, Any] | Responder | None = None, *,
                 default: Any = None, model: str = DEFAULT_CHAT_MODEL):
        self.responses, self.default, self.model = responses or {}, default, model
        self.usage = UsageMeter()
        self.calls: list[dict[str, Any]] = []

    def _pick(self, messages: list[Message], json_schema: dict | None) -> Any:
        if callable(self.responses):
            return self.responses(messages, json_schema)
        hay = "\n".join(str(m.get("content", "")) for m in messages)
        if json_schema and json_schema.get("title"):
            hay += "\n" + str(json_schema["title"])
        for key, value in self.responses.items():
            if key in hay:
                return value
        return self.default if self.default is not None else {}

    async def complete(self, messages: list[Message], *, model: str | None = None,
                       json_schema: dict | None = None, max_tokens: int = 1024,
                       temperature: float | None = None) -> str:
        self.calls.append({"messages": messages, "json_schema": json_schema, "model": model or self.model})
        value = self._pick(messages, json_schema)
        text = value if isinstance(value, str) else json.dumps(value)
        n_in = sum(len(str(m.get("content", ""))) for m in messages) // 4
        self.usage.add(n_in, len(text) // 4, price_for(model or self.model))
        return text

    async def complete_json(self, messages: list[Message], *, model: str | None = None,
                            json_schema: dict | None = None, max_tokens: int = 1024,
                            temperature: float | None = None) -> dict:
        return extract_json(await self.complete(messages, model=model, json_schema=json_schema,
                                                max_tokens=max_tokens, temperature=temperature))

    async def stream(self, messages: list[Message], *, model: str | None = None,
                     json_schema: dict | None = None, max_tokens: int = 1024,
                     temperature: float | None = None) -> AsyncIterator[str]:
        """Same text as `complete`, delivered in ~3 chunks so early-prefix parsers can be exercised."""
        self.calls.append({"messages": messages, "json_schema": json_schema, "model": model or self.model,
                           "stream": True})
        value = self._pick(messages, json_schema)
        text = value if isinstance(value, str) else json.dumps(value)
        n_in = sum(len(str(m.get("content", ""))) for m in messages) // 4
        self.usage.add(n_in, len(text) // 4, price_for(model or self.model))
        step = max(1, -(-len(text) // 3))
        for i in range(0, len(text), step):
            await asyncio.sleep(0)
            yield text[i:i + step]


class MockMuseTranscribe:
    """Replays scripted turns [{"speaker": "S0", "text": ..., "t_start": .., "t_end": ..}, ...] as
    speaker_change -> partial(s) -> final -> turn_end. `speed=0` replays instantly; `speed=1` paces
    the events in real time according to t_start/t_end; `speed=2` twice as fast, etc."""

    def __init__(self, turns: list[dict[str, Any]], *, speed: float = 0.0, partials_per_turn: int = 2,
                 mode: str = "DIARIZATION", **_: Any):
        self.turns, self.speed, self.partials_per_turn, self.mode = turns, speed, partials_per_turn, mode
        self.bytes_sent = 0
        self.session_id = "mock-session"
        self.t_last_audio: float | None = None
        self._ended = asyncio.Event()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def send_pcm(self, pcm16: bytes) -> None:
        self.bytes_sent += len(pcm16)

    async def end_stream(self) -> None:
        self._ended.set()

    async def events(self) -> AsyncIterator[TranscriptEvent]:
        clock = 0.0
        last_speaker: str | None = None
        for i, turn in enumerate(self.turns):
            spk, text = turn.get("speaker"), str(turn["text"])
            t0, t1 = float(turn.get("t_start", clock)), float(turn.get("t_end", clock + 1.0))
            raw = {"mock": True, "turn": turn}
            await self._wait(t0 - clock)
            clock = t0
            if spk is not None and spk != last_speaker:
                last_speaker = spk
                yield TranscriptEvent("speaker_change", "", spk, t0, t0, i, raw)
            words = text.split()
            n = max(1, min(self.partials_per_turn, len(words)))
            for k in range(1, n + 1):
                cut = len(words) * k // (n + 1)
                if cut == 0:
                    continue
                t = t0 + (t1 - t0) * k / (n + 1)
                await self._wait(t - clock)
                clock = t
                yield TranscriptEvent("partial", " ".join(words[:cut]), spk, t0, t, i, raw)
            await self._wait(t1 - clock)
            clock = t1
            yield TranscriptEvent("final", text, spk, t0, t1, i, raw)
            yield TranscriptEvent("turn_end", "", spk, t0, t1, i, raw)

    async def _wait(self, seconds: float) -> None:
        if self.speed > 0 and seconds > 0:
            await asyncio.sleep(seconds / self.speed)
