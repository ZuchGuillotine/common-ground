"""Offline interpreter: deterministic templates per mismatch type.

Used (a) when LLAMA_API_KEY is absent, (b) as the degraded path if Muse exceeds the stale deadline,
and (c) as the text-only / no-LLM ablation baseline in the evaluation. It is intentionally dumb:
its job is to make the whole loop runnable and to give the Muse interpreter something to beat.
"""
from __future__ import annotations

from typing import Optional

from ..schema import GraphEvent, Hypothesis, Intervention, MismatchType, Outcome, SessionConfig, Strategy
from .graph import ConversationGraph
from .nominator import Nomination

TEMPLATES: dict[MismatchType, Optional[Intervention]] = {
    MismatchType.TERM_AMBIGUITY: Intervention(
        strategy=Strategy.CLARIFY_DEFINITION,
        suggestion="Define the term they queried in plain words and say what stays unchanged.",
        earpiece_cue="Define the term",
        success_test="Counterpart uses the term correctly or stops asking about it."),
    MismatchType.POLITE_CONFUSION: Intervention(
        strategy=Strategy.ASK_CHECK_QUESTION,
        suggestion="Ask them to say back, in their words, what they think you're proposing.",
        earpiece_cue="Ask them to restate",
        success_test="Counterpart restates the proposal accurately or names the specific confusion."),
    MismatchType.UNANSWERED_OBJECTION: Intervention(
        strategy=Strategy.ACKNOWLEDGE_OBJECTION,
        suggestion="Name their question directly and answer only that before adding anything else.",
        earpiece_cue="Answer their question",
        success_test="Counterpart does not repeat the question."),
    MismatchType.INDIRECT_REFUSAL: Intervention(
        strategy=Strategy.LABEL_EMOTION,
        suggestion="Say: it sounds like there's a concern here you haven't said out loud yet.",
        earpiece_cue="Name the hesitation",
        success_test="Counterpart states a concrete concern or constraint."),
    MismatchType.ESCALATING_FRICTION: Intervention(
        strategy=Strategy.SLOW_DOWN,
        suggestion="Stop explaining; ask what part feels wrong to them and listen.",
        earpiece_cue="Stop. Ask them.",
        success_test="Counterpart gives a longer, calmer answer that names the sticking point."),
    MismatchType.GENUINE_DISAGREEMENT: Intervention(
        strategy=Strategy.ACKNOWLEDGE_OBJECTION,
        suggestion="Acknowledge that they understand and disagree; ask what would change their mind.",
        earpiece_cue="They disagree. Acknowledge.",
        success_test="Counterpart names a condition or trade-off rather than restating the objection."),
    MismatchType.DISTRACTION: None,
    MismatchType.NONE: None,
}


class FallbackInterpreter:
    """Same interface as engine.interpreter.Interpreter, no network."""

    async def interpret(self, cfg: SessionConfig, graph: ConversationGraph, nom: Nomination):
        hyp = Hypothesis(type=nom.type, summary=nom.reason, evidence_turns=nom.evidence_turns,
                         certainty="tentative" if nom.score < 0.7 else "moderate",
                         alternatives=[a.value for a in nom.alternatives])
        iv = TEMPLATES.get(nom.type)
        if nom.score < 0.5:
            return hyp, None
        return hyp, (iv.model_copy() if iv else None)

    async def judge_outcome(self, cfg: SessionConfig, graph: ConversationGraph, pending: GraphEvent) -> tuple[Outcome, str]:
        last = graph.last_turn("counterpart")
        if last is None or last.turn_id <= pending.turn_id:
            return "pending", "no counterpart turn yet"
        L = last.linguistic
        if L.repeated_question or L.clarification_request or L.explicit_disagreement:
            return "unresolved", f"turn #{last.turn_id} still shows a problem signal"
        if len(last.text.split()) >= 6 and not L.is_question:
            return "resolved", f"turn #{last.turn_id} is a substantive non-question"
        return "pending", "not enough evidence yet"
