"""The conversation graph: an ordered record of turns and the coaching events attached to them.

Four things are kept separate on every event: what was observed, what was hypothesised,
what was recommended, and what happened next. Persisted as JSONL so a session can be replayed
offline for evaluation and used later as (interaction -> repair -> outcome) training data.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator, Optional

from ..schema import CoachState, GraphEvent, Hypothesis, Intervention, Outcome, Turn


class ConversationGraph:
    def __init__(self, session_id: str, persist_dir: Optional[Path] = None):
        self.session_id = session_id
        self.turns: list[Turn] = []
        self.events: list[GraphEvent] = []
        self._path = (persist_dir / f"{session_id}.jsonl") if persist_dir else None
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    # ----------------------------------------------------------------- turns
    def add_turn(self, turn: Turn) -> Turn:
        if turn.turn_id is None or turn.turn_id < 0:
            turn.turn_id = len(self.turns)
        self.turns.append(turn)
        return turn

    def next_turn_id(self) -> int:
        return len(self.turns)

    def with_provisional(self, turn: Turn) -> "ConversationGraph":
        """A read-only view with `turn` appended (for a Tier 1 call started on a partial transcript)."""
        g = ConversationGraph(self.session_id)
        g.turns, g.events = [*self.turns, turn], self.events
        return g

    def recent(self, n: int = 8) -> list[Turn]:
        return self.turns[-n:]

    def turns_by(self, speaker: str) -> list[Turn]:
        return [t for t in self.turns if t.speaker == speaker]

    def last_turn(self, speaker: Optional[str] = None) -> Optional[Turn]:
        for t in reversed(self.turns):
            if speaker is None or t.speaker == speaker:
                return t
        return None

    # ---------------------------------------------------------------- events
    def record(
        self,
        turn: Turn,
        state_after: CoachState,
        interpretation: Optional[Hypothesis] = None,
        intervention: Optional[Intervention] = None,
        outcome: Outcome = "no_intervention",
    ) -> GraphEvent:
        ev = GraphEvent(
            turn_id=turn.turn_id,
            speaker=turn.speaker,
            transcript=turn.text,
            signals=self._signals_of(turn),
            interpretation=interpretation,
            intervention=intervention,
            outcome=outcome,
            state_after=state_after,
        )
        self.events.append(ev)
        self._append(ev)
        return ev

    def open_intervention(self) -> Optional[GraphEvent]:
        """The most recent event whose intervention is still awaiting an outcome."""
        for ev in reversed(self.events):
            if ev.intervention and ev.outcome == "pending":
                return ev
        return None

    def close_intervention(self, ev: GraphEvent, outcome: Outcome) -> None:
        ev.outcome = outcome
        self._append({"type": "outcome", "turn_id": ev.turn_id, "outcome": outcome})

    # ------------------------------------------------------------ summaries
    def compact_transcript(self, n: int = 10) -> str:
        """Short, token-cheap context for the interpreter."""
        lines = []
        for t in self.recent(n):
            flags = [k for k, v in t.linguistic.model_dump().items() if v is True]
            tone = ""
            if t.acoustic_emotion and not t.coaching_overlap:
                tone = f" [voice~{t.acoustic_emotion.label}]"
            if t.acoustic and t.acoustic.pitch_delta_vs_baseline is not None:
                d = t.acoustic.pitch_delta_vs_baseline
                if abs(d) >= 2:
                    tone += f" [pitch {'+' if d > 0 else ''}{d:.0f}st]"
            lines.append(f"#{t.turn_id} {t.speaker}: {t.text}" + (f"  {{{', '.join(flags)}}}" if flags else "") + tone)
        return "\n".join(lines)

    @staticmethod
    def _signals_of(turn: Turn) -> dict:
        sig = {k: v for k, v in turn.linguistic.model_dump().items() if v not in (False, None, [])}
        if turn.text_emotion:
            sig["text_emotion"] = turn.text_emotion.top[:3]
        if turn.acoustic_emotion:
            sig["acoustic_emotion"] = turn.acoustic_emotion.label
        if turn.acoustic:
            sig["acoustic"] = {k: round(v, 2) for k, v in turn.acoustic.model_dump().items() if isinstance(v, float)}
        if turn.coaching_overlap:
            sig["coaching_overlap"] = True
        return sig

    def _append(self, obj) -> None:
        if not self._path:
            return
        data = obj.model_dump(mode="json") if hasattr(obj, "model_dump") else obj
        with self._path.open("a") as f:
            f.write(json.dumps(data) + "\n")

    @staticmethod
    def iter_jsonl(path: Path) -> Iterator[dict]:
        with Path(path).open() as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)
