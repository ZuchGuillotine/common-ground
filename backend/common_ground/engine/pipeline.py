"""Glue: transcript events + raw audio -> annotated Turns -> Coach.

Owns speaker mapping (diarization labels S0/S1 -> wearer/counterpart), a rolling PCM ring buffer
so each finished turn can be sliced for acoustic analysis, and the fan-out to the signal models.
The acoustic emotion model is optional and runs off the critical path.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

import numpy as np

from ..schema import Speaker, Turn
from .coach import Coach

SR = 16000


class PcmRing:
    """Keeps the last `seconds` of int16 mono PCM with absolute timestamps."""

    def __init__(self, seconds: float = 60.0, sr: int = SR):
        self.sr = sr
        self.buf = np.zeros(int(seconds * sr), dtype=np.int16)
        self.t_end = 0.0        # stream time (s) of the last sample written
        self.total = 0

    def write(self, pcm: bytes) -> None:
        a = np.frombuffer(pcm, dtype=np.int16)
        n = len(a)
        if n >= len(self.buf):
            self.buf[:] = a[-len(self.buf):]
        else:
            self.buf = np.roll(self.buf, -n)
            self.buf[-n:] = a
        self.total += n
        self.t_end = self.total / self.sr

    def slice(self, t0: float, t1: float) -> Optional[np.ndarray]:
        t_start_buf = self.t_end - len(self.buf) / self.sr
        if t1 <= t_start_buf or t0 >= self.t_end:
            return None
        i0 = max(0, int((t0 - t_start_buf) * self.sr))
        i1 = min(len(self.buf), int((t1 - t_start_buf) * self.sr))
        return self.buf[i0:i1].copy() if i1 > i0 else None


class Pipeline:
    def __init__(self, coach: Coach, linguistic, text_emotion=None, acoustic=None, speech_emotion=None,
                 speaker_map: Optional[dict[str, Speaker]] = None):
        self.coach = coach
        self.linguistic = linguistic
        self.text_emotion = text_emotion
        self.acoustic = acoustic
        self.speech_emotion = speech_emotion
        self.speaker_map: dict[str, Speaker] = dict(speaker_map or {})
        self.ring = PcmRing()
        self._lock = asyncio.Lock()
        self._partials: dict[str, str] = {}
        self.latency_log: list[dict[str, float]] = []

    # ------------------------------------------------------------ audio in
    def feed_pcm(self, pcm: bytes) -> None:
        self.ring.write(pcm)

    def assign_speakers(self, mapping: dict[str, Speaker]) -> None:
        self.speaker_map.update(mapping)

    def map_speaker(self, label: Optional[str]) -> Speaker:
        if not label:
            return "unknown"
        if label in self.speaker_map:
            return self.speaker_map[label]
        # Heuristic default: the first diarized speaker is the wearer (they usually start the session).
        self.speaker_map[label] = "wearer" if "wearer" not in self.speaker_map.values() else "counterpart"
        return self.speaker_map[label]

    def guess_partial_speaker(self, label: Optional[str]) -> Speaker:
        """Who is speaking during a partial. In DIARIZATION mode the `speaker` event arrives only at the END
        of a turn, so a partial still carries the previous turn's label (verified live). Use the alternation
        prior instead: the person who did not speak last. Wrong for back-to-back turns by one speaker, which
        the final transcript corrects (a speculative Tier 1 call is then discarded)."""
        last = self.coach.graph.last_turn()
        if last is None or last.speaker == "unknown":
            return self.map_speaker(label)
        return "counterpart" if last.speaker == "wearer" else "wearer"

    async def on_partial_text(self, speaker: Speaker, text: str, t_start: float, t_now: float) -> None:
        """A partial transcript for the current turn (cumulative text). Cheap: rules only."""
        async with self._lock:
            ling = self.linguistic.analyze(text, speaker, self.coach.graph.turns) if speaker == "counterpart" else None
            await self.coach.on_partial(speaker, text, t_start, t_now, ling)

    # ------------------------------------------------------- transcript in
    async def on_transcript_event(self, ev: Any) -> Optional[Turn]:
        """ev: muse.transcribe.TranscriptEvent (duck-typed: kind, text, speaker_label, t_start, t_end)."""
        if ev.kind == "partial":
            self._partials[ev.speaker_label or "?"] = ev.text
            speaker = self.guess_partial_speaker(ev.speaker_label)
            await self.coach.emit("transcript", {"speaker": speaker, "text": ev.text, "final": False})
            await self.on_partial_text(speaker, ev.text, ev.t_start, ev.t_end)
            return None
        if ev.kind in ("final", "turn_end") and ev.text and ev.text.strip():
            self._partials.pop(ev.speaker_label or "?", None)
            return await self.on_text_turn(self.map_speaker(ev.speaker_label), ev.text.strip(), ev.t_start, ev.t_end)
        return None

    async def on_text_turn(self, speaker: Speaker, text: str, t_start: float, t_end: float) -> Turn:
        async with self._lock:
            t0 = time.monotonic()
            g = self.coach.graph
            turn = Turn(turn_id=g.next_turn_id(), speaker=speaker, text=text, t_start=t_start, t_end=t_end)
            turn.linguistic = self.linguistic.analyze(text, speaker, g.turns)
            if self.text_emotion is not None:
                turn.text_emotion = await asyncio.to_thread(self.text_emotion.classify, text)
            pcm = self.ring.slice(t_start, t_end) if self.acoustic is not None else None
            if pcm is not None and len(pcm) > SR // 4:
                turn.acoustic = await asyncio.to_thread(self.acoustic.features, pcm, SR, speaker)
                if self.speech_emotion is not None and len(pcm) >= SR:
                    try:
                        turn.acoustic_emotion = await asyncio.wait_for(
                            asyncio.to_thread(self.speech_emotion.classify, pcm, SR), timeout=1.5)
                    except Exception:
                        turn.acoustic_emotion = None   # off the critical path; never block a cue on it
            t_signals = time.monotonic() - t0
            await self.coach.on_turn(turn)
            self.latency_log.append({"turn_id": turn.turn_id, "signals_s": t_signals, "total_s": time.monotonic() - t0})
            return turn
