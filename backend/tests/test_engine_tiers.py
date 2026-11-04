"""Two-tier coaching: fixed-vocabulary Tier 0, evidence-gated Tier 1, held delivery, speculation."""
import asyncio
import pytest

from common_ground.schema import CoachState, LinguisticSignals, MismatchType, SessionConfig, Strategy, Turn
from common_ground.engine import Coach, ConversationGraph, Nominator, CUE_VOCABULARY, EvidenceAccumulator
from common_ground.engine.interpreter import EarlyCue, Interpreter, parse_early
from common_ground.engine.nominator import Nomination
from common_ground.muse import MockMuseChat


def mk(turn_id, speaker, text, **flags):
    t = Turn(turn_id=turn_id, speaker=speaker, text=text, t_start=turn_id * 5.0, t_end=turn_id * 5.0 + 4)
    t.linguistic = LinguisticSignals(**flags)
    return t


class Clock:
    t = 0.0
    def __call__(self): return self.t


class SlowInterpreter:
    """Streams the early fields after `early_delay`, finishes after `total_delay`. Records calls."""
    def __init__(self, early_delay=0.0, total_delay=0.0, act=True):
        self.early_delay, self.total_delay, self.act = early_delay, total_delay, act
        self.calls = []

    async def interpret(self, cfg, graph, nom, *, on_early=None):
        self.calls.append(nom)
        await asyncio.sleep(self.early_delay)
        if on_early:
            await on_early(EarlyCue(self.act, Strategy.ASK_CHECK_QUESTION, "Check their picture"))
        await asyncio.sleep(max(0.0, self.total_delay - self.early_delay))
        from common_ground.schema import Hypothesis, Intervention
        hyp = Hypothesis(type=nom.type, summary="m", certainty="moderate")
        if not self.act:
            return hyp, None
        return hyp, Intervention(strategy=Strategy.ASK_CHECK_QUESTION, suggestion="Ask what they think you proposed.",
                                 earpiece_cue="Check their picture", success_test="They restate it.")

    async def judge_outcome(self, cfg, graph, pending):
        return "pending", ""


def make(interp, clock, **kw):
    cfg = SessionConfig(session_id="t", min_seconds_between_cues=0, consent_confirmed=True)
    events = []
    async def emit(kind, payload): events.append((kind, payload))
    coach = Coach(cfg, ConversationGraph("t"), interp, Nominator(), emit, clock=clock, **kw)
    return coach, events


def cues(events, tier=None):
    return [p for k, p in events if k == "cue" and (tier is None or p["tier"] == tier)]


# ------------------------------------------------------------------ early parse
def test_parse_early_fires_only_when_three_fields_complete():
    assert parse_early('{"act": true, "cue": "Ask them to') is None
    assert parse_early('{"act": true, "cue": "Ask them to restate", "how": "ask_check') is None
    ec = parse_early('{"act":true,"cue":"Ask them to restate please now","how":"ask_check_question","say":')
    assert ec == EarlyCue(True, Strategy.ASK_CHECK_QUESTION, "Ask them to restate")   # capped at 4 words
    ec = parse_early('{"act": false, "cue": "", "how": "stay_silent", "say": "", ')
    assert ec.intervene is False and ec.earpiece_cue == ""


async def test_interpreter_streams_and_fires_early_from_mock():
    chat = MockMuseChat(default={"act": True, "cue": "Define the term", "how": "clarify_definition",
                                 "say": "Say what escrow means in one line.", "sure": "moderate",
                                 "test": "They use the term correctly.", "type": "term_ambiguity"})
    interp = Interpreter(chat)
    g = ConversationGraph("t"); g.add_turn(mk(0, "wearer", "Escrow protects both sides."))
    g.add_turn(mk(1, "counterpart", "Escrow?", is_question=True, jargon_terms=["escrow"]))
    nom = Nomination(MismatchType.TERM_AMBIGUITY, "queried term", [1], [], 0.75)
    seen = []
    hyp, iv = await interp.interpret(SessionConfig(session_id="t"), g, nom, on_early=lambda ec: seen.append(ec))
    assert chat.calls[-1]["stream"] is True
    assert seen and seen[0].earpiece_cue == "Define the term" and seen[0].strategy == Strategy.CLARIFY_DEFINITION
    assert iv is not None and iv.suggestion.startswith("Say what escrow") and hyp.type == MismatchType.TERM_AMBIGUITY


# ------------------------------------------------------------------ tier 0
async def test_single_weak_nomination_gets_tier0_only():
    interp = SlowInterpreter()
    coach, events = make(interp, Clock())
    await coach.on_turn(mk(0, "wearer", "We integrate with your existing workflow."))
    ev = await coach.on_turn(mk(1, "counterpart", "Sure. Would we have to replace everything?",
                                explicit_agreement=True, is_question=True, clarification_request=True))
    await coach.drain()
    assert ev.intervention is not None and ev.intervention.earpiece_cue == CUE_VOCABULARY["polite_confusion"]
    assert [c["tier"] for c in cues(events)] == [0]
    assert interp.calls == [] and coach.metrics.tier1_calls == 0
    assert coach.state == CoachState.AWAITING_RESPONSE


async def test_accumulated_evidence_escalates_to_tier1():
    interp = SlowInterpreter()
    coach, events = make(interp, Clock())
    await coach.on_turn(mk(0, "wearer", "Escrow protects both sides."))
    await coach.on_turn(mk(1, "counterpart", "Escrow? What does that mean?", is_question=True, jargon_terms=["escrow"]))
    assert coach.metrics.tier1_calls == 0            # 0.75 < threshold
    await coach.on_turn(mk(2, "wearer", "Money held by a third party."))
    await coach.on_turn(mk(3, "counterpart", "Hmm, that might be difficult for finance.", soft_refusal=True))
    await coach.drain()
    assert coach.metrics.tier1_calls == 1 and interp.calls[0].type == MismatchType.INDIRECT_REFUSAL
    t1 = cues(events, 1)
    assert len(t1) == 1 and t1[0]["earpiece_cue"] == "Check their picture" and t1[0]["turn_id"] == 3
    assert [k for k, _ in events if k == "cue_detail"] == ["cue_detail"]
    # the Tier 1 intervention supersedes its Tier 0 twin in the graph
    pend = [e for e in coach.graph.events if e.turn_id == 3 and e.intervention and e.outcome == "pending"]
    assert len(pend) == 1 and pend[0].intervention.suggestion.startswith("Ask what")


async def test_strong_single_nomination_escalates_immediately():
    interp = SlowInterpreter()
    coach, events = make(interp, Clock())
    await coach.on_turn(mk(0, "wearer", "It syncs every night."))
    await coach.on_turn(mk(1, "counterpart", "So when does it sync?", is_question=True))
    await coach.on_turn(mk(2, "wearer", "Every night, at two."))
    await coach.on_turn(mk(3, "counterpart", "Right, but when does it sync?", is_question=True, repeated_question=True, repeat_of_turn_id=1))
    await coach.on_turn(mk(4, "wearer", "Nightly."))
    await coach.on_turn(mk(5, "counterpart", "When does it sync though?", is_question=True, repeated_question=True, repeat_of_turn_id=1))
    await coach.drain()
    assert coach.metrics.tier1_calls >= 1
    assert interp.calls[-1].type == MismatchType.ESCALATING_FRICTION


async def test_tier1_always_runs_model_on_every_nomination():
    interp = SlowInterpreter()
    coach, events = make(interp, Clock(), tier1_always=True)
    await coach.on_turn(mk(0, "wearer", "Escrow protects both sides."))
    await coach.on_turn(mk(1, "counterpart", "Escrow?", is_question=True, jargon_terms=["escrow"]))
    await coach.drain()
    assert coach.metrics.tier1_calls == 1 and [c["tier"] for c in cues(events)] == [0, 1]


# ------------------------------------------------------------------ delivery
async def test_tier1_held_while_wearer_speaks_then_delivered_at_boundary():
    interp = SlowInterpreter(early_delay=0.02, total_delay=0.03)
    coach, events = make(interp, Clock(), tier1_always=True)
    await coach.on_turn(mk(0, "wearer", "Escrow protects both sides."))
    await coach.on_turn(mk(1, "counterpart", "Escrow?", is_question=True, jargon_terms=["escrow"]))
    await coach.on_partial("wearer", "it means the money is", 5.0, 5.5)     # wearer starts answering
    await asyncio.sleep(0.06)                                               # model finishes meanwhile
    assert cues(events, 1) == [] and coach.metrics.tier1_held == 1
    await coach.on_turn(mk(2, "wearer", "It means the money is held by a third party."))
    assert len(cues(events, 1)) == 1                                        # delivered at the boundary


async def test_tier1_dropped_when_stale():
    clock = Clock()
    interp = SlowInterpreter(early_delay=0.02, total_delay=0.02)
    coach, events = make(interp, clock, tier1_always=True, stale_after_s=8.0)
    await coach.on_turn(mk(0, "wearer", "Escrow protects both sides."))
    await coach.on_turn(mk(1, "counterpart", "Escrow?", is_question=True, jargon_terms=["escrow"]))
    await coach.on_partial("wearer", "well", 5.0, 5.5)
    await asyncio.sleep(0.05)
    clock.t = 20.0                                                           # the moment has passed
    await coach.on_turn(mk(2, "wearer", "Long explanation that took a while."))
    await coach.drain()
    assert cues(events, 1) == [] and coach.metrics.tier1_dropped == 1


async def test_tier1_silent_is_recorded_not_cued():
    interp = SlowInterpreter(act=False)
    coach, events = make(interp, Clock(), tier1_always=True)
    await coach.on_turn(mk(0, "wearer", "Escrow protects both sides."))
    await coach.on_turn(mk(1, "counterpart", "Escrow?", is_question=True, jargon_terms=["escrow"]))
    await coach.drain()
    assert [c["tier"] for c in cues(events)] == [0] and coach.metrics.tier1_silent == 1


# ------------------------------------------------------------------ speculation
async def test_speculative_start_kept_when_final_matches():
    interp = SlowInterpreter(early_delay=0.01, total_delay=0.02)
    coach, events = make(interp, Clock(), tier1_always=True)
    await coach.on_turn(mk(0, "wearer", "Escrow protects both sides."))
    L = LinguisticSignals(is_question=True, jargon_terms=["escrow"])
    await coach.on_partial("counterpart", "wait what is escrow exactly", 5.0, 6.0, L)
    assert coach.metrics.speculative_started == 1 and coach.metrics.tier1_calls == 1
    await asyncio.sleep(0.04)                      # model finishes before the turn ends
    assert cues(events, 1) == []                   # not verified yet: held
    await coach.on_turn(mk(1, "counterpart", "Wait, what is escrow exactly, in practice?", is_question=True, jargon_terms=["escrow"]))
    await coach.drain()
    assert coach.metrics.speculative_kept == 1 and coach.metrics.tier1_calls == 1
    assert [c["tier"] for c in cues(events)] == [0, 1]
    assert coach.metrics.tier1_ready_after_turn_s and coach.metrics.tier1_ready_after_turn_s[0] < 0.05


async def test_speculative_start_discarded_when_final_differs():
    interp = SlowInterpreter(early_delay=0.5, total_delay=0.6)
    coach, events = make(interp, Clock(), tier1_always=True)
    await coach.on_turn(mk(0, "wearer", "Escrow protects both sides."))
    L = LinguisticSignals(is_question=True, jargon_terms=["escrow"])
    await coach.on_partial("counterpart", "wait what is escrow", 5.0, 6.0, L)
    assert coach.metrics.speculative_started == 1
    # final turn says something else entirely: no nomination
    await coach.on_turn(mk(1, "counterpart", "Never mind, I found the page, it all makes sense now.", explicit_agreement=True))
    await coach.drain()
    assert coach.metrics.speculative_discarded == 1 and cues(events) == []


def test_evidence_accumulator_decays_and_escalates():
    acc = EvidenceAccumulator(threshold=1.0, immediate=0.9, decay=0.5)
    weak = Nomination(MismatchType.POLITE_CONFUSION, "r", [], [], 0.7)
    acc.observe(weak); assert acc.should_escalate(weak)[0] is False
    acc.observe(None); assert acc.score == pytest.approx(0.35)
    acc.observe(weak); assert acc.should_escalate(weak)[0] is True
    acc.reset()
    strong = Nomination(MismatchType.ESCALATING_FRICTION, "r", [], [], 0.9)
    assert acc.would_escalate(strong) is True
    acc.observe(weak); acc.unresolved()
    assert acc.would_escalate(Nomination(MismatchType.INDIRECT_REFUSAL, "r", [], [], 0.55)) is True
