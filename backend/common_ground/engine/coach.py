"""The coach: state machine + two-tier cue scheduler. One instance per session.

LISTENING -> POSSIBLE_MISMATCH -> REPAIR_SUGGESTED -> AWAITING_RESPONSE -> RESOLVED | UNRESOLVED -> LISTENING

Two tiers (see engine/evidence.py):
  * Tier 0: every nomination gets a fixed-vocabulary earpiece cue (fallback.TEMPLATES) at once, no model.
  * Tier 1: when the EvidenceAccumulator says the moment deserves it, Muse Spark runs in the background
    and its cue is delivered at the next listening window (nobody speaking), never mid-utterance, and
    dropped rather than delivered late. Tier 1 may be started speculatively on a counterpart's partial
    transcript and is kept only if the final transcript confirms the same nomination.

Rules enforced here (not in the model):
  * cues only at turn boundaries, never while anyone is mid-utterance
  * at most one cue per `min_seconds_between_cues` (a Tier 1 cue may follow its own Tier 0 twin)
  * outcome judging of a delivered cue never blocks the hot path (bounded inline wait, then background)
  * stale Tier 1 suggestions (older than `stale_after_s`, or two counterpart turns later) are dropped
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from ..schema import (CoachState, GraphEvent, Hypothesis, Intervention, LinguisticSignals, SessionConfig,
                      Speaker, Turn)
from .evidence import EvidenceAccumulator
from .fallback import TEMPLATES
from .graph import ConversationGraph
from .interpreter import EarlyCue, Interpreter
from .nominator import Nomination, Nominator

Emit = Callable[[str, dict], Awaitable[None]]   # (message type, payload)

# The complete Tier 0 earpiece vocabulary. Deterministic, benign if wrong, <= 4 words each.
CUE_VOCABULARY: dict[str, str] = {k.value: v.earpiece_cue for k, v in TEMPLATES.items() if v is not None}


@dataclass
class CoachMetrics:
    turns: int = 0
    nominations: int = 0
    cues: int = 0                      # all tiers
    tier0_cues: int = 0
    tier1_calls: int = 0               # Muse (or fallback) interpret calls started
    tier1_cues: int = 0
    tier1_silent: int = 0              # model said do not act
    tier1_held: int = 0                # ready while someone was speaking; delivered at the next boundary
    tier1_dropped: int = 0             # stale / superseded / throttled
    tier1_errors: int = 0
    speculative_started: int = 0
    speculative_kept: int = 0
    speculative_discarded: int = 0
    suppressed_by_throttle: int = 0
    resolved: int = 0
    unresolved: int = 0
    interpret_latency_s: list[float] = field(default_factory=list)       # wall time of the full Tier 1 call
    tier1_early_s: list[float] = field(default_factory=list)             # wall time until act/cue/how parsed
    tier1_ready_after_turn_s: list[float] = field(default_factory=list)  # cue ready minus trigger turn end (wall)

    @property
    def interpretations(self) -> int:   # backward-compatible name
        return self.tier1_calls

    def summary(self) -> dict:
        def pct(xs, q):
            xs = sorted(xs)
            return round(xs[min(len(xs) - 1, int(q * len(xs)))], 3) if xs else None
        lists = {"interpret_latency_s", "tier1_early_s", "tier1_ready_after_turn_s"}
        return {**{k: v for k, v in self.__dict__.items() if k not in lists},
                "interpret_p50_s": pct(self.interpret_latency_s, 0.5), "interpret_p95_s": pct(self.interpret_latency_s, 0.95),
                "tier1_early_p50_s": pct(self.tier1_early_s, 0.5),
                "tier1_ready_after_turn_p50_s": pct(self.tier1_ready_after_turn_s, 0.5),
                "tier1_ready_after_turn_p95_s": pct(self.tier1_ready_after_turn_s, 0.95)}


@dataclass
class _Tier1:
    nom: Nomination
    turn_id: int                         # trigger turn (the provisional id when speculative)
    speculative_text: Optional[str]      # partial the call was started on; None when started at a final
    started_wall: float
    task: Optional[asyncio.Task] = None
    verified: bool = False               # final transcript confirmed the speculative nomination
    trigger_clock: Optional[float] = None   # coach clock when the trigger turn completed
    final_wall: Optional[float] = None
    early: Optional[EarlyCue] = None
    hypothesis: Optional[Hypothesis] = None
    intervention: Optional[Intervention] = None
    done: bool = False
    delivered: bool = False
    held_logged: bool = False
    dropped: bool = False
    event: Optional[GraphEvent] = None


class Coach:
    def __init__(self, cfg: SessionConfig, graph: ConversationGraph, interpreter: Interpreter,
                 nominator: Optional[Nominator] = None, emit: Optional[Emit] = None,
                 stale_after_s: float = 8.0, clock: Callable[[], float] = time.monotonic, *,
                 evidence: Optional[EvidenceAccumulator] = None, tier1_always: bool = False,
                 speculate: bool = True, judge_inline_s: float = 0.05, min_partial_words: int = 4):
        self.cfg = cfg
        self.graph = graph
        self.interpreter = interpreter
        self.nominator = nominator or Nominator()
        self.emit = emit or (lambda *_: _noop())
        self.evidence = evidence or EvidenceAccumulator()
        self.tier1_always = tier1_always
        self.speculate = speculate
        self.judge_inline_s = judge_inline_s
        self.min_partial_words = min_partial_words
        self.state = CoachState.LISTENING
        self.metrics = CoachMetrics()
        self._last_cue_at: float = -1e9
        self._last_cue_turn_id: Optional[int] = None
        self._stale_after = stale_after_s
        self._clock = clock
        self.cue_windows: list[tuple[float, float]] = []   # (t_start, t_end) of played cues, for echo masking
        self._speaking: dict[str, bool] = {}
        self._tier1: Optional[_Tier1] = None
        self._spec_started_turn: Optional[int] = None
        self._judge_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------- lifecycle
    async def on_partial(self, speaker: Speaker, text: str, t_start: float, t_now: float,
                         linguistic: Optional[LinguisticSignals] = None) -> None:
        """A partial transcript: marks who is speaking and may start Tier 1 speculatively."""
        self._speaking = {speaker: True}
        if speaker != "counterpart" or not self.speculate:
            return
        if self._tier1 is not None and not self._tier1.done:
            return
        tid = self.graph.next_turn_id()
        if self._spec_started_turn == tid or len(text.split()) < self.min_partial_words:
            return
        prov = Turn(turn_id=tid, speaker="counterpart", text=text, t_start=t_start, t_end=t_now,
                    linguistic=linguistic or LinguisticSignals())
        nom = self.nominator.nominate(prov, self.graph)
        if nom is None or not (self.tier1_always or self.evidence.would_escalate(nom)):
            return
        if self._throttled(self._clock(), tid):
            return
        self._spec_started_turn = tid
        self.metrics.speculative_started += 1
        self._start_tier1(nom, prov, self.graph.with_provisional(prov), speculative_text=text)

    async def on_turn(self, turn: Turn) -> GraphEvent:
        """Entry point for every completed turn (after the signals layer has annotated it)."""
        self.metrics.turns += 1
        now = self._clock()
        turn.coaching_overlap = any(s <= turn.t_end and turn.t_start <= e for s, e in self.cue_windows)
        self.graph.add_turn(turn)
        self._speaking = {}
        await self.emit("turn", turn.model_dump(mode="json"))

        if turn.speaker != "counterpart":
            # A finished wearer turn is a listening window for a held Tier 1 cue.
            await self._flush_held()
            if self.state in (CoachState.RESOLVED, CoachState.UNRESOLVED, CoachState.POSSIBLE_MISMATCH):
                await self._set_state(CoachState.LISTENING)
            return self.graph.record(turn, self.state)

        # 1. Judge any pending intervention using this counterpart turn (never blocks for long).
        pending = self.graph.open_intervention()
        if pending is not None:
            await self._judge(pending, turn)

        # 2. Nominate (rules, <5 ms).
        nom = self.nominator.nominate(turn, self.graph)
        self._reconcile_speculative(turn, nom, now)
        if nom is None:
            self.evidence.observe(None)
            await self._flush_held()
            if self.state in (CoachState.RESOLVED, CoachState.UNRESOLVED, CoachState.POSSIBLE_MISMATCH):
                await self._set_state(CoachState.LISTENING)
            return self.graph.record(turn, self.state)
        self.metrics.nominations += 1
        await self._set_state(CoachState.POSSIBLE_MISMATCH, {"candidate": nom.type.value, "reason": nom.reason})

        # 3. Tier 0: fixed-vocabulary cue, immediately, subject to the throttle.
        hyp = Hypothesis(type=nom.type, summary=nom.reason, evidence_turns=nom.evidence_turns,
                         certainty="tentative" if nom.score < 0.7 else "moderate",
                         alternatives=[a.value for a in nom.alternatives])
        template = TEMPLATES.get(nom.type)
        delivered0 = False
        if template is None:
            ev = self.graph.record(turn, self.state, interpretation=hyp)
        elif self._throttled(now, turn.turn_id):
            self.metrics.suppressed_by_throttle += 1
            await self._set_state(CoachState.LISTENING, {"dropped": "throttled", "tier": 0})
            ev = self.graph.record(turn, self.state, interpretation=hyp, intervention=template.model_copy(), outcome="superseded")
        else:
            iv0 = template.model_copy()
            self._last_cue_at, self._last_cue_turn_id = now, turn.turn_id
            self.metrics.cues += 1
            self.metrics.tier0_cues += 1
            delivered0 = True
            await self._set_state(CoachState.REPAIR_SUGGESTED, {"tier": 0})
            await self.emit("cue", self._cue_payload(0, turn.turn_id, iv0, hyp))
            ev = self.graph.record(turn, CoachState.AWAITING_RESPONSE, interpretation=hyp, intervention=iv0, outcome="pending")
            await self._set_state(CoachState.AWAITING_RESPONSE)

        # 4. Tier 1: spend a model call only when the evidence warrants it (or a speculative one is verified).
        self.evidence.observe(nom)
        t1 = self._tier1
        if t1 is not None and t1.verified and t1.turn_id == turn.turn_id:
            self.evidence.reset()                      # the speculative call covers this moment
        elif delivered0 or self.tier1_always:
            fire, why = self.evidence.should_escalate(nom)
            if (fire or self.tier1_always) and (t1 is None or t1.done):
                self.evidence.reset()
                await self._set_state(self.state, {"tier1": "started", "why": why})
                self._start_tier1(nom, turn, self.graph)
        await self._flush_held()
        return ev

    def on_cue_played(self, t_start: float, t_end: float) -> None:
        self.cue_windows.append((t_start, t_end))

    async def wait_background(self) -> None:
        """Wait for in-flight Tier 1 / judging tasks (replay harnesses; a live session never calls this)."""
        tasks = [t for t in ([self._tier1.task] if self._tier1 and self._tier1.task else []) + list(self._judge_tasks) if t]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def drain(self) -> None:
        """Wait for background work and deliver anything still held (end of session)."""
        await self.wait_background()
        self._speaking = {}
        await self._flush_held(end_of_session=True)

    # ---------------------------------------------------------------- tier 1
    def _start_tier1(self, nom: Nomination, turn: Turn, graph_view: ConversationGraph, *,
                     speculative_text: Optional[str] = None) -> None:
        now_wall = time.monotonic()
        t1 = _Tier1(nom=nom, turn_id=turn.turn_id, speculative_text=speculative_text, started_wall=now_wall)
        if speculative_text is None:
            t1.verified, t1.trigger_clock, t1.final_wall = True, self._clock(), now_wall
        self._tier1 = t1
        self.metrics.tier1_calls += 1
        t1.task = asyncio.create_task(self._run_tier1(t1, graph_view))

    async def _run_tier1(self, t1: _Tier1, graph_view: ConversationGraph) -> None:
        async def on_early(ec: EarlyCue) -> None:
            t1.early = ec
            self.metrics.tier1_early_s.append(time.monotonic() - t1.started_wall)
            if ec.intervene and ec.earpiece_cue:
                await self._try_deliver(t1)
        try:
            hyp, iv = await self.interpreter.interpret(self.cfg, graph_view, t1.nom, on_early=on_early)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - a Tier 1 failure must never take the session down
            self.metrics.tier1_errors += 1
            t1.done = True
            await self._set_state(self.state, {"tier1": "error", "error": str(e)[:200]})
            return
        self.metrics.interpret_latency_s.append(time.monotonic() - t1.started_wall)
        t1.hypothesis, t1.intervention, t1.done = hyp, iv, True
        if iv is None:
            self.metrics.tier1_silent += 1
            if t1.delivered:   # early fields said act, the full object did not: keep the cue, log it
                await self._set_state(self.state, {"tier1": "contradicted", "turn_id": t1.turn_id})
            else:
                await self._set_state(self.state, {"tier1": "silent", "turn_id": t1.turn_id,
                                                   "hypothesis": hyp.model_dump(mode="json")})
            return
        if not t1.delivered and not t1.dropped:
            await self._try_deliver(t1)
        if t1.delivered:
            await self._emit_detail(t1)

    def _reconcile_speculative(self, turn: Turn, nom: Optional[Nomination], now: float) -> None:
        """At a final counterpart turn: keep or discard a Tier 1 call started on its partial."""
        t1 = self._tier1
        if t1 is None or t1.speculative_text is None or t1.verified or t1.turn_id != turn.turn_id:
            return
        if nom is not None and nom.type == t1.nom.type and _text_compatible(t1.speculative_text, turn.text):
            t1.verified, t1.trigger_clock, t1.final_wall = True, now, time.monotonic()
            t1.nom = nom
            self.metrics.speculative_kept += 1
        else:
            self.metrics.speculative_discarded += 1
            t1.dropped = True
            if t1.task is not None and not t1.task.done():
                t1.task.cancel()
            t1.done = True
            self._tier1 = None

    async def _try_deliver(self, t1: _Tier1) -> bool:
        """Deliver the Tier 1 cue if the moment allows; otherwise hold (or drop if stale)."""
        if t1.delivered or t1.dropped:
            return t1.delivered
        if not t1.verified or any(self._speaking.values()):
            if not t1.held_logged:
                t1.held_logged = True
                self.metrics.tier1_held += 1
            return False
        now = self._clock()
        reason = None
        assert t1.trigger_clock is not None
        if now - t1.trigger_clock > self._stale_after:
            reason = "stale"
        elif sum(1 for t in self.graph.turns if t.speaker == "counterpart" and t.turn_id > t1.turn_id) >= 2:
            reason = "superseded"
        elif self._throttled(now, t1.turn_id):
            reason = "throttled"
        if reason:
            t1.dropped = True
            self.metrics.tier1_dropped += 1
            await self._set_state(self.state, {"tier1": "dropped", "reason": reason, "turn_id": t1.turn_id,
                                               "latency_s": round(time.monotonic() - t1.started_wall, 3)})
            if t1.event is not None:
                t1.event.outcome = "superseded"
            return False
        cue = t1.intervention.earpiece_cue if t1.intervention else t1.early.earpiece_cue   # type: ignore[union-attr]
        strategy = t1.intervention.strategy if t1.intervention else t1.early.strategy       # type: ignore[union-attr]
        iv = t1.intervention or Intervention(strategy=strategy, suggestion="", earpiece_cue=cue, success_test="")
        hyp = t1.hypothesis or Hypothesis(type=t1.nom.type, summary=t1.nom.reason, evidence_turns=t1.nom.evidence_turns,
                                          alternatives=[a.value for a in t1.nom.alternatives])
        t1.delivered = True
        self._last_cue_at, self._last_cue_turn_id = now, t1.turn_id
        self.metrics.cues += 1
        self.metrics.tier1_cues += 1
        if t1.final_wall is not None:
            self.metrics.tier1_ready_after_turn_s.append(time.monotonic() - t1.final_wall)
        await self._set_state(CoachState.REPAIR_SUGGESTED, {"tier": 1})
        await self.emit("cue", self._cue_payload(1, t1.turn_id, iv, hyp))
        for ev in self.graph.events:   # the Tier 0 twin is superseded by the richer intervention
            if ev.turn_id == t1.turn_id and ev.intervention is not None and ev.outcome == "pending":
                ev.outcome = "superseded"
        trigger = next((t for t in self.graph.turns if t.turn_id == t1.turn_id), self.graph.turns[-1])
        t1.event = self.graph.record(trigger, CoachState.AWAITING_RESPONSE, interpretation=hyp, intervention=iv, outcome="pending")
        await self._set_state(CoachState.AWAITING_RESPONSE)
        return True

    async def _emit_detail(self, t1: _Tier1) -> None:
        """The full suggestion for the phone screen, once the stream has completed."""
        assert t1.intervention is not None and t1.hypothesis is not None
        if t1.event is not None:
            t1.event.intervention, t1.event.interpretation = t1.intervention, t1.hypothesis
        await self.emit("cue_detail", {"tier": 1, "turn_id": t1.turn_id, **t1.intervention.model_dump(mode="json"),
                                       "hypothesis": t1.hypothesis.model_dump(mode="json")})

    async def _flush_held(self, end_of_session: bool = False) -> None:
        t1 = self._tier1
        if t1 is None or t1.delivered or t1.dropped:
            return
        if end_of_session and not t1.verified:
            t1.dropped = True
            self.metrics.tier1_dropped += 1
            return
        ready = t1.intervention is not None or (t1.early is not None and t1.early.intervene)
        if ready and await self._try_deliver(t1) and t1.intervention is not None:
            await self._emit_detail(t1)

    # ---------------------------------------------------------------- judging
    async def _judge(self, pending: GraphEvent, turn: Turn) -> None:
        task = asyncio.create_task(self._judge_task(pending, turn))
        self._judge_tasks.add(task)
        task.add_done_callback(self._judge_tasks.discard)
        await asyncio.wait({task}, timeout=self.judge_inline_s)   # fallback judges instantly; Muse continues in background

    async def _judge_task(self, pending: GraphEvent, turn: Turn) -> None:
        try:
            outcome, evidence = await self.interpreter.judge_outcome(self.cfg, self.graph, pending)
        except Exception as e:  # noqa: BLE001
            await self._set_state(self.state, {"judge": "error", "error": str(e)[:200]})
            return
        if outcome == "pending" or pending.outcome != "pending":
            return
        self.graph.close_intervention(pending, outcome)
        self.metrics.resolved += outcome == "resolved"
        self.metrics.unresolved += outcome == "unresolved"
        if outcome == "unresolved":
            self.evidence.unresolved()      # the next nomination escalates sooner
        await self._set_state(CoachState.RESOLVED if outcome == "resolved" else CoachState.UNRESOLVED,
                              {"turn_id": pending.turn_id, "outcome": outcome, "evidence": evidence})

    # ---------------------------------------------------------------- helpers
    def _throttled(self, now: float, turn_id: int) -> bool:
        if self._last_cue_turn_id == turn_id:   # a Tier 1 cue may follow its own Tier 0 twin
            return False
        return now - self._last_cue_at < self.cfg.min_seconds_between_cues

    def _cue_payload(self, tier: int, turn_id: int, iv: Intervention, hyp: Hypothesis) -> dict:
        return {"tier": tier, "turn_id": turn_id, "earpiece_cue": iv.earpiece_cue, "suggestion": iv.suggestion,
                "strategy": iv.strategy.value, "success_test": iv.success_test, "hypothesis": hyp.model_dump(mode="json")}

    async def _set_state(self, s: CoachState, extra: Optional[dict] = None) -> None:
        self.state = s
        await self.emit("state", {"state": s.value, **(extra or {})})


_WORD = re.compile(r"[a-z0-9']+")


def _text_compatible(partial: str, final: str) -> bool:
    """The final transcript confirms the partial a speculative call was started on."""
    p, f = _WORD.findall(partial.lower()), _WORD.findall(final.lower())
    if not p:
        return False
    if f[:len(p)] == p:
        return True
    overlap = len(set(p) & set(f)) / len(set(p))
    return overlap >= 0.8


async def _noop():
    return None
