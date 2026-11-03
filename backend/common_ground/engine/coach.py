"""The coach: state machine + cue scheduler. One instance per session.

LISTENING -> POSSIBLE_MISMATCH -> REPAIR_SUGGESTED -> AWAITING_RESPONSE -> RESOLVED | UNRESOLVED -> LISTENING

Rules enforced here (not in the model):
  * cues only at completed turns, never while the counterpart is mid-utterance
  * at most one cue per `min_seconds_between_cues`
  * a pending intervention is judged on the next counterpart turn before any new cue
  * stale suggestions (older than `stale_after_s`) are dropped, not delivered late
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from ..schema import CoachState, GraphEvent, Hypothesis, Intervention, SessionConfig, Turn
from .graph import ConversationGraph
from .interpreter import Interpreter
from .nominator import Nominator

Emit = Callable[[str, dict], Awaitable[None]]   # (message type, payload)


@dataclass
class CoachMetrics:
    turns: int = 0
    nominations: int = 0
    interpretations: int = 0
    cues: int = 0
    suppressed_by_throttle: int = 0
    suppressed_by_model: int = 0
    resolved: int = 0
    unresolved: int = 0
    interpret_latency_s: list[float] = field(default_factory=list)

    def summary(self) -> dict:
        lat = sorted(self.interpret_latency_s)
        p = lambda q: (lat[min(len(lat) - 1, int(q * len(lat)))] if lat else None)
        return {**{k: v for k, v in self.__dict__.items() if k != "interpret_latency_s"},
                "interpret_p50_s": p(0.5), "interpret_p95_s": p(0.95)}


class Coach:
    def __init__(self, cfg: SessionConfig, graph: ConversationGraph, interpreter: Interpreter,
                 nominator: Optional[Nominator] = None, emit: Optional[Emit] = None,
                 stale_after_s: float = 8.0, clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg
        self.graph = graph
        self.interpreter = interpreter
        self.nominator = nominator or Nominator()
        self.emit = emit or (lambda *_: _noop())
        self.state = CoachState.LISTENING
        self.metrics = CoachMetrics()
        self._last_cue_at: float = -1e9
        self._stale_after = stale_after_s
        self._clock = clock
        self.cue_windows: list[tuple[float, float]] = []   # (t_start, t_end) of played cues, for echo masking

    # ------------------------------------------------------------- lifecycle
    async def on_turn(self, turn: Turn) -> GraphEvent:
        """Entry point for every completed turn (after the signals layer has annotated it)."""
        self.metrics.turns += 1
        turn.coaching_overlap = any(s <= turn.t_end and turn.t_start <= e for s, e in self.cue_windows)
        self.graph.add_turn(turn)
        await self.emit("turn", turn.model_dump(mode="json"))

        # 1. Judge any pending intervention using this counterpart turn.
        pending = self.graph.open_intervention()
        if pending and turn.speaker == "counterpart":
            outcome, evidence = await self.interpreter.judge_outcome(self.cfg, self.graph, pending)
            if outcome != "pending":
                self.graph.close_intervention(pending, outcome)
                self.metrics.resolved += outcome == "resolved"
                self.metrics.unresolved += outcome == "unresolved"
                await self._set_state(CoachState.RESOLVED if outcome == "resolved" else CoachState.UNRESOLVED,
                                      {"turn_id": pending.turn_id, "outcome": outcome, "evidence": evidence})
                pending = None
            else:
                return self.graph.record(turn, self.state)

        # 2. Nominate.
        nom = self.nominator.nominate(turn, self.graph)
        if not nom:
            if self.state in (CoachState.RESOLVED, CoachState.UNRESOLVED, CoachState.POSSIBLE_MISMATCH):
                await self._set_state(CoachState.LISTENING)
            return self.graph.record(turn, self.state)
        self.metrics.nominations += 1
        await self._set_state(CoachState.POSSIBLE_MISMATCH, {"candidate": nom.type.value, "reason": nom.reason})

        # 3. Interpret (the only Muse call on the hot path).
        t0 = self._clock()
        hyp, iv = await self.interpreter.interpret(self.cfg, self.graph, nom)
        dt = self._clock() - t0
        self.metrics.interpretations += 1
        self.metrics.interpret_latency_s.append(dt)

        if iv is None:
            self.metrics.suppressed_by_model += 1
            await self._set_state(CoachState.LISTENING, {"hypothesis": hyp.model_dump(mode="json"), "silent": True})
            return self.graph.record(turn, self.state, interpretation=hyp)

        # 4. Schedule the cue, subject to throttle and staleness.
        now = self._clock()
        if dt > self._stale_after:
            self.metrics.suppressed_by_throttle += 1
            await self._set_state(CoachState.LISTENING, {"dropped": "stale", "latency_s": dt})
            return self.graph.record(turn, self.state, interpretation=hyp, intervention=iv, outcome="superseded")
        if now - self._last_cue_at < self.cfg.min_seconds_between_cues:
            self.metrics.suppressed_by_throttle += 1
            await self._set_state(CoachState.LISTENING, {"dropped": "throttled"})
            return self.graph.record(turn, self.state, interpretation=hyp, intervention=iv, outcome="superseded")

        self._last_cue_at = now
        self.metrics.cues += 1
        await self._set_state(CoachState.REPAIR_SUGGESTED)
        await self.emit("cue", {**iv.model_dump(mode="json"), "hypothesis": hyp.model_dump(mode="json"),
                                "turn_id": turn.turn_id})
        ev = self.graph.record(turn, CoachState.AWAITING_RESPONSE, interpretation=hyp, intervention=iv, outcome="pending")
        await self._set_state(CoachState.AWAITING_RESPONSE)
        return ev

    def on_cue_played(self, t_start: float, t_end: float) -> None:
        self.cue_windows.append((t_start, t_end))

    async def _set_state(self, s: CoachState, extra: Optional[dict] = None) -> None:
        self.state = s
        await self.emit("state", {"state": s.value, **(extra or {})})


async def _noop():
    return None
