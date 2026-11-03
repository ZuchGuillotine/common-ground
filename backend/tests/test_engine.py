"""Engine tests use a stub detector so they do not depend on the signals layer."""
import asyncio
import pytest

from common_ground.schema import CoachState, LinguisticSignals, MismatchType, SessionConfig, Strategy, Turn
from common_ground.engine import Coach, ConversationGraph, Nominator
from common_ground.engine.fallback import FallbackInterpreter


def mk(turn_id, speaker, text, **flags):
    t = Turn(turn_id=turn_id, speaker=speaker, text=text, t_start=turn_id * 5.0, t_end=turn_id * 5.0 + 4)
    t.linguistic = LinguisticSignals(**flags)
    return t


class Clock:
    t = 0.0
    def __call__(self): return self.t


@pytest.fixture
def setup(tmp_path):
    cfg = SessionConfig(session_id="t", min_seconds_between_cues=20, consent_confirmed=True)
    events = []
    async def emit(kind, payload): events.append((kind, payload))
    clock = Clock()
    coach = Coach(cfg, ConversationGraph("t", tmp_path), FallbackInterpreter(), Nominator(), emit, clock=clock)
    return coach, events, clock


async def test_polite_confusion_cycle(setup):
    coach, events, clock = setup
    await coach.on_turn(mk(0, "wearer", "We integrate with your existing workflow."))
    await coach.on_turn(mk(1, "counterpart", "Yeah, that makes sense.", explicit_agreement=True))
    assert coach.state == CoachState.LISTENING
    clock.t = 10
    ev = await coach.on_turn(mk(2, "counterpart", "Sure. Would we have to replace everything?",
                                explicit_agreement=True, is_question=True, clarification_request=True))
    assert ev.intervention is not None and ev.outcome == "pending"
    assert ev.interpretation.type == MismatchType.POLITE_CONFUSION
    assert coach.state == CoachState.AWAITING_RESPONSE
    cues = [p for k, p in events if k == "cue"]
    assert len(cues) == 1 and len(cues[0]["earpiece_cue"].split()) <= 4
    # outcome judged on next substantive counterpart turn
    await coach.on_turn(mk(3, "wearer", "No replacement; your tools stay. Did I explain the distinction?"))
    await coach.on_turn(mk(4, "counterpart", "Ah okay, so it sits alongside what we already run, that's fine."))
    assert ev.outcome == "resolved"
    assert coach.metrics.resolved == 1
    # graph persisted
    lines = (coach.graph._path).read_text().splitlines()
    assert any('"outcome": "resolved"' in l for l in lines)


async def test_throttle_suppresses_second_cue(setup):
    coach, events, clock = setup
    await coach.on_turn(mk(0, "wearer", "Escrow protects both sides."))
    clock.t = 5
    await coach.on_turn(mk(1, "counterpart", "Escrow? What does that mean?", is_question=True, clarification_request=True, jargon_terms=["escrow"]))
    assert coach.metrics.cues == 1
    # pending intervention gets resolved by a substantive answer, then a new nomination arrives within 20 s
    await coach.on_turn(mk(2, "wearer", "Money held by a third party until both sign."))
    clock.t = 12
    await coach.on_turn(mk(3, "counterpart", "Right, held until we both sign, but that might be difficult for finance.", soft_refusal=True))
    assert coach.metrics.cues == 1
    assert coach.metrics.suppressed_by_throttle == 1


async def test_neutral_conversation_is_silent(setup):
    coach, events, clock = setup
    await coach.on_turn(mk(0, "wearer", "Lunch at one?", is_question=True))
    await coach.on_turn(mk(1, "counterpart", "Yeah, one works, see you downstairs.", explicit_agreement=True))
    await coach.on_turn(mk(2, "wearer", "Perfect."))
    await coach.on_turn(mk(3, "counterpart", "See you.", minimal_response=True))
    assert coach.metrics.cues == 0 and coach.metrics.nominations == 0


async def test_disagreement_not_treated_as_confusion(setup):
    coach, events, clock = setup
    await coach.on_turn(mk(0, "wearer", "The price includes a support year."))
    ev = await coach.on_turn(mk(1, "counterpart", "I understand, and I still don't think it's worth it.", explicit_disagreement=True))
    assert ev.interpretation.type == MismatchType.GENUINE_DISAGREEMENT
    if ev.intervention:
        assert ev.intervention.strategy != Strategy.CLARIFY_DEFINITION


async def test_cue_echo_marks_overlap(setup):
    coach, events, clock = setup
    coach.on_cue_played(4.5, 6.0)
    t = mk(0, "counterpart", "sorry what", is_question=True)
    t.t_start, t.t_end = 5.0, 7.0
    await coach.on_turn(t)
    assert coach.graph.turns[0].coaching_overlap is True
