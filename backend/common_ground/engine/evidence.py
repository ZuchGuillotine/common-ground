"""Evidence accumulator: decides when a nomination is worth a Muse call (Tier 1).

Two tiers of coaching:
  * Tier 0, every nomination: a fixed-vocabulary earpiece cue (fallback.TEMPLATES), deterministic,
    delivered within ~1 s of the counterpart finishing. It tells the wearer *that* a moment happened.
  * Tier 1, occasionally: Muse Spark reasons over the surrounding turns and writes one sentence.
    It costs 4-7 s, so it is spent only when the evidence has accumulated: the same kind of trouble
    sustains across turns, a Tier 0 cue demonstrably did not work (judged unresolved), or a single
    nomination is already strong (e.g. a question asked for the third time).

All thresholds live here so they can be tuned against the fixtures (scripts/eval_scenarios.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..schema import MismatchType
from .nominator import Nomination


@dataclass
class EvidenceAccumulator:
    threshold: float = 1.0          # accumulated score at which Tier 1 fires
    immediate: float = 0.9          # a single nomination at least this strong fires Tier 1 on its own
    decay: float = 0.5              # score multiplier per counterpart turn that produced no nomination
    streak_bonus: float = 0.2       # extra weight when the nomination repeats the previous type
    unresolved_bonus: float = 0.6   # added when a delivered cue's outcome is judged unresolved
    score: float = 0.0
    last_type: Optional[MismatchType] = None
    streak: int = 0
    log: list[str] = field(default_factory=list)

    # ------------------------------------------------------------- mutation
    def observe(self, nom: Optional[Nomination]) -> float:
        """Fold one completed counterpart turn into the score. Returns the new score."""
        if nom is None:
            self.score *= self.decay
            self.last_type, self.streak = None, 0
            return self.score
        self.score += self._weight(nom)
        self.streak = self.streak + 1 if nom.type == self.last_type else 1
        self.last_type = nom.type
        self.log.append(f"{nom.type.value}+{self._weight(nom):.2f}={self.score:.2f}")
        return self.score

    def unresolved(self) -> None:
        """A cue was delivered and the next counterpart turn still showed the problem."""
        self.score += self.unresolved_bonus
        self.log.append(f"unresolved+{self.unresolved_bonus:.2f}={self.score:.2f}")

    def reset(self) -> None:
        self.score, self.last_type, self.streak = 0.0, None, 0

    # -------------------------------------------------------------- queries
    def should_escalate(self, nom: Nomination) -> tuple[bool, str]:
        """Call after observe(nom). Whether to spend a Tier 1 call on this nomination, and why."""
        if nom.score >= self.immediate:
            return True, f"strong_nomination({nom.score:.2f})"
        if self.score >= self.threshold:
            return True, f"accumulated({self.score:.2f}>={self.threshold})"
        return False, f"below_threshold({self.score:.2f})"

    def would_escalate(self, nom: Nomination) -> bool:
        """Pure preview of should_escalate for a nomination not yet observed (speculative partials)."""
        return nom.score >= self.immediate or (self.score + self._weight(nom)) >= self.threshold

    def _weight(self, nom: Nomination) -> float:
        return nom.score + (self.streak_bonus if nom.type == self.last_type else 0.0)
