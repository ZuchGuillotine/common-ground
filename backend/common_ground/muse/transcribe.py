"""Streaming client for Muse Voice Transcribe over WebSocket.

Wire protocol (see docs/MUSE_API.md for the full write-up with sources):
  * URL        wss://api.meta.ai/v1/asr/realtime        https://dev.meta.ai/docs/api-reference/voice/realtime
  * Handshake  first JSON text frame, within 10 s; the API key goes in the handshake, NOT in a header
  * Audio      raw PCM16 LE mono, binary frames, paced at ~real time (server rejects >5 s backlog)
  * End        {"type": "endStream"} then keep reading until the server closes with 1000
  * Events     speechStart / transcript / speaker / speechEnd / speechComplete / audioProgress / error
               https://dev.meta.ai/docs/api-reference/voice/schemas
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK

from .base import MuseError, TranscribeMode, TranscriptEvent
from .chat import api_key_from_env
from .models import DEFAULT_TRANSCRIBE_MODEL, REALTIME_ASR_URL

PCM_16KHZ_BYTES_PER_SEC = 32_000   # 16 kHz * 2 bytes, mono
HANDSHAKE_TIMEOUT_S = 10.0

# UNVERIFIED (no API key in the authoring session): the docs say `speaker.label` is a bare letter
# such as "A"; we map labels to "S0", "S1", ... in first-seen order. If labels turn out to be
# something else (e.g. "SPEAKER_00"), only this normaliser needs to change.
def normalise_speaker_label(label: str, seen: dict[str, str]) -> str:
    return seen.setdefault(str(label), f"S{len(seen)}")


class MuseTranscribeError(MuseError):
    pass


_SENTINEL: Any = object()


@dataclass
class _Turn:
    turn_id: int | None
    t_start: float | None = None
    t_end: float | None = None
    speaker: str | None = None
    partial: str = ""


@dataclass
class TranscriptNormaliser:
    """Pure state machine: raw server messages -> normalised TranscriptEvents. Unit-testable."""
    speakers: dict[str, str] = field(default_factory=dict)
    turns: dict[int | None, _Turn] = field(default_factory=dict)
    current: _Turn | None = None
    last_speaker: str | None = None

    def feed(self, raw: dict[str, Any]) -> list[TranscriptEvent]:
        kind = raw.get("type")
        t = _ms(raw.get("audioProcessedMs"))
        tid = raw.get("turnId")
        out: list[TranscriptEvent] = []
        if kind == "speechStart":
            self.current = self.turns[tid] = _Turn(tid, t_start=t, speaker=self.last_speaker)
        elif kind == "transcript":
            turn = self._turn_for(tid)
            text = str(raw.get("transcript", ""))
            turn.partial = text
            if raw.get("final"):   # PUSH_TO_TALK completion signal
                turn.t_end = t
                out += self._finish(turn, text, raw)
            else:
                out.append(TranscriptEvent("partial", text, turn.speaker, turn.t_start, t, turn.turn_id, raw))
        elif kind == "speaker":    # labels the span that preceded this event
            label = normalise_speaker_label(raw.get("label", "?"), self.speakers)
            turn = self._turn_for(tid)
            turn.speaker = label
            if label != self.last_speaker:
                self.last_speaker = label
                out.append(TranscriptEvent("speaker_change", "", label, turn.t_start, t, turn.turn_id, raw))
        elif kind == "speechEnd":
            self._turn_for(tid).t_end = t
        elif kind == "speechComplete":   # may arrive late: match by turnId, not arrival order
            turn = self._turn_for(tid)
            turn.t_end = turn.t_end if turn.t_end is not None else t
            out += self._finish(turn, str(raw.get("transcript", "")), raw)
        elif kind == "error":
            raise MuseTranscribeError(f"server error: {raw.get('message')} "
                                      f"(code={raw.get('errorCode')}, type={raw.get('errorType')})")
        # audioProgress and unknown types are ignored, as the docs instruct
        return out

    def _turn_for(self, tid: int | None) -> _Turn:
        if tid is not None and tid in self.turns:
            return self.turns[tid]
        if self.current is None or (tid is not None and self.current.turn_id != tid):
            self.current = self.turns[tid] = _Turn(tid, speaker=self.last_speaker)
        return self.current

    def _finish(self, turn: _Turn, text: str, raw: dict) -> list[TranscriptEvent]:
        self.turns.pop(turn.turn_id, None)
        if self.current is turn:
            self.current = None
        args = (turn.speaker, turn.t_start, turn.t_end, turn.turn_id, raw)
        return [TranscriptEvent("final", text, *args), TranscriptEvent("turn_end", "", *args)]


def _ms(v: Any) -> float | None:
    return None if v is None else float(v) / 1000.0


class MuseTranscribe:
    """Usage:
        async with MuseTranscribe(mode="DIARIZATION") as t:
            sender = asyncio.create_task(pump(t))        # await t.send_pcm(chunk) ...; await t.end_stream()
            async for ev in t.events(): ...
    `pace=True` throttles send_pcm to real time (needed when replaying a file; the server closes
    with 1008 if the client gets more than ~5 s ahead).
    """

    def __init__(self, mode: TranscribeMode = "DIARIZATION", *, api_key: str | None = None,
                 url: str | None = None, model: str = DEFAULT_TRANSCRIBE_MODEL,
                 keywords: list[str] | None = None, language_bias: list[str] | None = None,
                 pace: bool = False, emit_audio_progress: bool = False):
        self.api_key = api_key or api_key_from_env()
        if not self.api_key:
            raise MuseTranscribeError("LLAMA_API_KEY (or MODEL_API_KEY) is not set")
        self.url = url or os.environ.get("LLAMA_ASR_REALTIME_URL") or REALTIME_ASR_URL
        self.mode, self.model = mode, model
        self.keywords, self.language_bias = keywords, language_bias
        self.pace, self.emit_audio_progress = pace, emit_audio_progress
        self.session_id: str | None = None
        self.bytes_sent = 0
        self.t_last_audio: float | None = None       # time.monotonic() of the last send_pcm
        self._ws: ClientConnection | None = None
        self._queue: asyncio.Queue[TranscriptEvent | BaseException | object] = asyncio.Queue()
        self._reader: asyncio.Task | None = None
        self._pace_t0: float | None = None
        self._norm = TranscriptNormaliser()

    # ------------------------------------------------------------- lifecycle
    async def __aenter__(self) -> "MuseTranscribe":
        self._ws = await connect(self.url, max_size=None)
        await self._ws.send(json.dumps(self.handshake()))
        try:
            ack = json.loads(await asyncio.wait_for(self._ws.recv(), HANDSHAKE_TIMEOUT_S))
        except (asyncio.TimeoutError, ConnectionClosed) as e:
            raise MuseTranscribeError(f"handshake failed: {e}") from e
        if not isinstance(ack, dict) or ack.get("type") == "error" or not ack.get("sessionId"):
            raise MuseTranscribeError(f"handshake rejected: {ack}")
        self.session_id = ack["sessionId"]
        self._reader = asyncio.create_task(self._read_loop())
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._reader and not self._reader.done():
            self._reader.cancel()
        if self._ws:
            await self._ws.close()

    def handshake(self) -> dict[str, Any]:
        # https://dev.meta.ai/docs/api-reference/voice/schemas (RealtimeHandshake)
        hs: dict[str, Any] = {
            "authorization": {"accessToken": f"Bearer {self.api_key}"},
            "audioEncoding": "PCM_16KHZ",
            "model": self.model,
            "mode": self.mode,
            "partialMode": "CUMULATIVE",
            "emitAudioProgress": self.emit_audio_progress,
        }
        if self.keywords:
            hs["keywords"] = self.keywords
        if self.language_bias:
            hs["languageBias"] = self.language_bias
        return hs

    # --------------------------------------------------------------- sending
    async def send_pcm(self, pcm16: bytes) -> None:
        assert self._ws is not None, "use `async with`"
        if self.pace:
            now = time.monotonic()
            self._pace_t0 = self._pace_t0 or now
            ahead = (self.bytes_sent / PCM_16KHZ_BYTES_PER_SEC) - (now - self._pace_t0)
            if ahead > 0:
                await asyncio.sleep(ahead)
        await self._ws.send(pcm16)
        self.bytes_sent += len(pcm16)
        self.t_last_audio = time.monotonic()

    async def end_stream(self) -> None:
        assert self._ws is not None
        try:
            await self._ws.send(json.dumps({"type": "endStream"}))
        except ConnectionClosed:
            pass

    # -------------------------------------------------------------- receiving
    async def events(self) -> AsyncIterator[TranscriptEvent]:
        while True:
            item = await self._queue.get()
            if item is _SENTINEL:
                return
            if isinstance(item, BaseException):
                raise item
            yield item  # type: ignore[misc]

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if isinstance(msg, (bytes, bytearray)):
                    continue
                raw = json.loads(msg)
                for ev in self._norm.feed(raw):
                    self._queue.put_nowait(ev)
        except ConnectionClosedOK:
            pass
        except ConnectionClosed as e:   # 1008 bad request/pacing, 1011 backend, 1013 rate limited
            self._queue.put_nowait(MuseTranscribeError(f"websocket closed {e.rcvd.code if e.rcvd else '?'}: "
                                                       f"{e.rcvd.reason if e.rcvd else e}"))
        except Exception as e:  # noqa: BLE001 - surface anything to the consumer
            self._queue.put_nowait(e)
        finally:
            self._queue.put_nowait(_SENTINEL)
