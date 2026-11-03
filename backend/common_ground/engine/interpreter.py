"""Muse Spark as the contextual interpreter and intervention author.

Called only at completed counterpart turns that the Nominator flagged, or to judge the outcome
of a pending intervention. It receives compact state, must weigh competing explanations, and is
explicitly allowed to answer "insufficient evidence" / stay silent.
"""
from __future__ import annotations

import json
from typing import Any, Optional, Protocol

from ..schema import (Certainty, CoachState, GraphEvent, Hypothesis, Intervention, MismatchType,
                      Outcome, SessionConfig, Strategy)
from .graph import ConversationGraph
from .nominator import Nomination


class JsonChat(Protocol):
    async def complete_json(self, messages: list[dict[str, Any]], *, json_schema: Optional[dict] = None,
                            max_tokens: int = 400, temperature: float = 0.2) -> dict: ...


SYSTEM = """You are the interpretation layer of Common Ground, a communication copilot worn by one
speaker (the "wearer") talking with a "counterpart". You never claim to read minds. You reason
about OBSERVABLE evidence of failed understanding, misaligned meaning, or friction, keep competing
explanations visible, and prefer helping the wearer ASK a good question over asserting what the
counterpart feels. Disagreement is not misunderstanding. Voice/tone signals are weak evidence and
never override what was actually said. If evidence is weak or the conversation is going fine,
say so and recommend silence. Suggestions must be concrete, one sentence, sayable in the next
breath, and must serve mutual understanding and voluntary agreement, never manipulation."""

INTERPRET_SCHEMA = {
    "type": "object",
    "properties": {
        "mismatch_type": {"type": "string", "enum": [m.value for m in MismatchType]},
        "summary": {"type": "string"},
        "alternatives": {"type": "array", "items": {"type": "string"}},
        "certainty": {"type": "string", "enum": ["none", "tentative", "moderate", "strong"]},
        "evidence_turns": {"type": "array", "items": {"type": "integer"}},
        "intervene": {"type": "boolean"},
        "strategy": {"type": "string", "enum": [s.value for s in Strategy]},
        "suggestion": {"type": "string"},
        "earpiece_cue": {"type": "string"},
        "success_test": {"type": "string"},
        "rationale": {"type": "string"},
    },
    "required": ["mismatch_type", "summary", "alternatives", "certainty", "evidence_turns",
                 "intervene", "strategy", "suggestion", "earpiece_cue", "success_test", "rationale"],
    "additionalProperties": False,
}

OUTCOME_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": ["resolved", "unresolved", "pending"]},
        "evidence": {"type": "string"},
    },
    "required": ["outcome", "evidence"],
    "additionalProperties": False,
}

STRATEGY_HINTS = {
    MismatchType.TERM_AMBIGUITY: "clarify_definition or ask_check_question",
    MismatchType.POLITE_CONFUSION: "ask_check_question (invite them to restate), or switch_frame",
    MismatchType.UNANSWERED_OBJECTION: "acknowledge_objection, then answer the actual question",
    MismatchType.INDIRECT_REFUSAL: "label_emotion or invite_no; do not push",
    MismatchType.ESCALATING_FRICTION: "slow_down, label_emotion, or mirror; NOT more explanation",
    MismatchType.GENUINE_DISAGREEMENT: "acknowledge_objection; do not re-explain as if they misunderstood",
    MismatchType.DISTRACTION: "stay_silent or ask_check_question",
    MismatchType.NONE: "stay_silent",
}


def _goal_line(cfg: SessionConfig) -> str:
    s = f"Wearer's goal: {cfg.goal.value}."
    if cfg.intended_message:
        s += f' Intended message: "{cfg.intended_message}".'
    return s


class Interpreter:
    def __init__(self, chat: JsonChat):
        self.chat = chat

    async def interpret(self, cfg: SessionConfig, graph: ConversationGraph, nom: Nomination
                        ) -> tuple[Hypothesis, Optional[Intervention]]:
        user = f"""{_goal_line(cfg)}

Recent conversation (signals in braces are rule detections; [voice~x] is a weak acoustic estimate):
{graph.compact_transcript(10)}

A rule nominated a possible issue on turn #{graph.turns[-1].turn_id}:
  candidate: {nom.type.value}  because: {nom.reason}
  competing explanations to weigh: {', '.join(a.value for a in nom.alternatives)}
Usual strategy for this candidate: {STRATEGY_HINTS.get(nom.type, 'stay_silent')}.

Decide: what is the best-supported explanation, what are the alternatives, how certain are you,
and should the wearer be given a cue right now? If intervene is false, strategy must be stay_silent.
earpiece_cue is at most 4 words. suggestion is one sentence the wearer could say or do next.
success_test describes what the counterpart's NEXT turn would show if the repair worked."""
        out = await self.chat.complete_json(
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
            json_schema=INTERPRET_SCHEMA,
        )
        hyp = Hypothesis(
            type=MismatchType(out.get("mismatch_type", nom.type.value)),
            summary=out.get("summary", nom.reason),
            evidence_turns=[int(i) for i in out.get("evidence_turns", nom.evidence_turns)],
            certainty=_cert(out.get("certainty")),
            alternatives=list(out.get("alternatives", [])),
        )
        if not out.get("intervene") or out.get("strategy") == Strategy.STAY_SILENT.value or hyp.certainty == "none":
            return hyp, None
        return hyp, Intervention(
            strategy=_strategy(out.get("strategy")),
            suggestion=out.get("suggestion", "").strip(),
            earpiece_cue=" ".join(out.get("earpiece_cue", "").split()[:4]),
            success_test=out.get("success_test", ""),
            rationale=out.get("rationale", ""),
        )

    async def judge_outcome(self, cfg: SessionConfig, graph: ConversationGraph, pending: GraphEvent) -> tuple[Outcome, str]:
        iv = pending.intervention
        assert iv is not None
        user = f"""{_goal_line(cfg)}

Earlier, on turn #{pending.turn_id}, the wearer was advised: "{iv.suggestion}"
Success test: {iv.success_test}

Conversation since then:
{graph.compact_transcript(6)}

Did the counterpart's subsequent turn(s) show the success test being met (resolved), clearly not met
(unresolved), or is it too early to tell (pending)? Cite the turn that decides it."""
        out = await self.chat.complete_json(
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
            json_schema=OUTCOME_SCHEMA, max_tokens=150,
        )
        o = out.get("outcome", "pending")
        return (o if o in ("resolved", "unresolved", "pending") else "pending"), out.get("evidence", "")


def _cert(v) -> Certainty:
    return v if v in ("none", "tentative", "moderate", "strong") else "tentative"


def _strategy(v) -> Strategy:
    try:
        return Strategy(v)
    except ValueError:
        return Strategy.ASK_CHECK_QUESTION
