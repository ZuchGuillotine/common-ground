"""Cheap, deterministic rules that NOMINATE a possible mismatch from observed signals.

A nomination is not a diagnosis. It gates the (slower, costlier) Muse interpreter so that the
reasoner only runs when there is something to reason about, and it names the competing
explanations the interpreter must weigh. All thresholds live here so they can be tuned
against the scenario fixtures (scripts/eval_scenarios.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..schema import MismatchType, Turn
from .graph import ConversationGraph


@dataclass
class Nomination:
    type: MismatchType
    reason: str
    evidence_turns: list[int] = field(default_factory=list)
    alternatives: list[MismatchType] = field(default_factory=list)
    score: float = 0.0   # 0..1, rule confidence that *something* is worth interpreting


class Nominator:
    def __init__(
        self,
        repeated_question_threshold: int = 2,
        wearer_long_turn_words: int = 45,
        friction_window: int = 4,
    ):
        self.repeated_question_threshold = repeated_question_threshold
        self.wearer_long_turn_words = wearer_long_turn_words
        self.friction_window = friction_window

    def nominate(self, turn: Turn, graph: ConversationGraph) -> Optional[Nomination]:
        if turn.speaker != "counterpart" or not turn.is_final:
            return None
        L = turn.linguistic
        prior_cp = [t for t in graph.turns_by("counterpart") if t.turn_id < turn.turn_id]
        prior_w = [t for t in graph.turns_by("wearer") if t.turn_id < turn.turn_id]

        # 1. Agreement words followed by a question that contradicts or probes the explanation.
        if L.explicit_agreement and (L.is_question or L.clarification_request):
            return Nomination(
                MismatchType.POLITE_CONFUSION,
                "agreement language immediately followed by a clarification question",
                [turn.turn_id] + ([prior_w[-1].turn_id] if prior_w else []),
                [MismatchType.GENUINE_DISAGREEMENT, MismatchType.NONE],
                0.7,
            )

        # 2. A question that repeats an earlier question: the earlier answer did not land.
        if L.repeated_question and L.repeat_of_turn_id is not None:
            n_repeats = sum(1 for t in prior_cp if t.linguistic.repeated_question) + 1
            typ = MismatchType.ESCALATING_FRICTION if n_repeats >= self.repeated_question_threshold else MismatchType.UNANSWERED_OBJECTION
            return Nomination(
                typ,
                f"question repeated ({n_repeats}x); previous explanation did not resolve it",
                [L.repeat_of_turn_id, turn.turn_id],
                [MismatchType.TERM_AMBIGUITY, MismatchType.GENUINE_DISAGREEMENT, MismatchType.DISTRACTION],
                min(0.5 + 0.2 * n_repeats, 0.95),
            )

        # 3. Counterpart queries a term the wearer introduced.
        if L.jargon_terms and (L.is_question or L.clarification_request):
            return Nomination(
                MismatchType.TERM_AMBIGUITY,
                f"counterpart queried wearer's term(s): {', '.join(L.jargon_terms)}",
                [turn.turn_id] + ([prior_w[-1].turn_id] if prior_w else []),
                [MismatchType.NONE],
                0.75,
            )

        # 4. Soft refusal / hedged agreement after a wearer proposal.
        if L.soft_refusal or (L.hedged_agreement and not L.is_question):
            return Nomination(
                MismatchType.INDIRECT_REFUSAL,
                "hedged or deferring language where a commitment was invited",
                [turn.turn_id],
                [MismatchType.GENUINE_DISAGREEMENT, MismatchType.NONE],
                0.55,
            )

        # 5. Explicit disagreement: understood, disagrees. Nominate so the coach does NOT
        #    mistake it for confusion and so it can offer acknowledgement rather than re-explanation.
        if L.explicit_disagreement:
            return Nomination(
                MismatchType.GENUINE_DISAGREEMENT,
                "explicit disagreement; treat as alignment problem, not comprehension",
                [turn.turn_id],
                [MismatchType.UNANSWERED_OBJECTION],
                0.6,
            )

        # 6. Friction: rising tone + minimal responses + wearer monologuing.
        recent = graph.recent(self.friction_window)
        minimal = sum(1 for t in recent if t.speaker == "counterpart" and t.linguistic.minimal_response)
        long_wearer = any(t.speaker == "wearer" and len(t.text.split()) > self.wearer_long_turn_words for t in recent)
        hot = (turn.acoustic and not turn.coaching_overlap and (turn.acoustic.pitch_delta_vs_baseline or 0) > 3
               and (turn.acoustic.energy_delta_vs_baseline or 0) > 3)
        if (minimal >= 2 and long_wearer) or (hot and (minimal or L.explicit_disagreement)):
            return Nomination(
                MismatchType.ESCALATING_FRICTION,
                "minimal responses and/or raised voice while wearer holds the floor",
                [t.turn_id for t in recent],
                [MismatchType.DISTRACTION, MismatchType.NONE],
                0.5,
            )
        return None
