"""Rule-based linguistic signal detector.

Pure regex + a token-Jaccard check for repeated questions. No models, no heavy deps,
sub-millisecond per turn. These signals *nominate* possible problems; they never diagnose.
Precision is preferred over recall throughout: every pattern is anchored to a clause start
or a fixed phrase so that casual mentions do not fire.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Optional

from common_ground.schema import LinguisticSignals, Speaker, Turn

# --------------------------------------------------------------------------- pattern table
# Each entry is a list of readable regex fragments. They are joined with "|" and compiled once,
# case-insensitively. `^` means "start of the utterance or of a clause" (see _CLAUSE_START).
# Keep fragments short and literal; add a new line rather than making an existing one cleverer.

_CLAUSE_START = r"(?:^|[.!?;:,]\s*|\s(?:and|but|so|well|um|uh|oh)\s+)"

PATTERNS: dict[str, list[str]] = {
    "clarification_request": [
        r"what do you mean",
        r"what does that mean",
        r"what('s| is) (?:a|an|the)?\s*\w+\?",          # "what's a tenant?"
        r"so you(?:'re| are) saying",
        r"so (?:you mean|basically|then|that means)",
        r"you mean\b",
        r"^wait[,.]?\s",
        r"^wait\?*$",
        r"sorry,? (?:what|how|why|which|when|say)",
        r"how does that",
        r"how would that",
        r"how is that (?:different|the same)",
        r"is that (?:the same|different)",
        r"do (?:we|i|you|they) (?:have|need) to",
        r"would (?:we|i|you|they) (?:have|need) to",
        r"will (?:we|i|you|they) (?:have|need) to",
        r"i(?:'m| am) not sure i (?:follow|understand|get)",
        r"i don(?:'t|’t) (?:follow|understand|get it)",
        r"i(?:'m| am) (?:a (?:bit|little) )?(?:confused|lost)",
        r"(?:can|could) you (?:explain|clarify|elaborate|repeat|rephrase)",
        r"(?:run|say) that (?:by me )?again",
        r"come again\??",
        r"in what sense",
        r"which (?:one|part|bit) ",
    ],
    "explicit_agreement": [
        r"^(?:yeah|yes|yep|yup|sure|ok(?:ay)?|right|exactly|absolutely|definitely|agreed|totally)\b",
        r"^(?:that |it )?makes sense\b",
        r"^that(?:'s| is) (?:right|true|fair|correct)\b",
        r"^(?:got it|i see|i agree|sounds good|fair enough|good point|understood)\b",
    ],
    "explicit_disagreement": [
        r"^(?:no|nope|nah)\b",
        r"^(?:well,? )?i don(?:'t|’t) think (?:so|that)",
        r"^i disagree\b",
        r"^that(?:'s| is) not\b",
        r"^that(?:'s| is)n't\b",
        r"^not really\b",
        r"^that won(?:'t|’t) work\b",
        r"^i(?:'m| am) not (?:convinced|sure about that)",
        r"^(?:actually|but),? (?:no|that(?:'s| is) not)\b",
    ],
    "hedged_agreement": [
        r"^(?:yeah|yes|sure|ok(?:ay)?|fine|right)[,.…]*\s*(?:i (?:guess|suppose)|maybe|whatever|if you say so|why not)",
        r"^i (?:guess|suppose)(?: so)?\b",
        r"^(?:yeah|yes|sure|ok(?:ay)?)\.{2,}",                   # "yeah..."
        r"^(?:yeah|yes|sure|ok(?:ay)?),? maybe\b",
        r"^(?:if you say so|whatever you (?:say|think))\b",
        r"^(?:i mean,? )?(?:maybe|possibly|perhaps|could be|kind of|sort of)\.?$",
    ],
    "soft_refusal": [
        r"(?:that|this|it) (?:might|may|could|would) be (?:difficult|hard|tricky|a problem|a stretch|tough)",
        r"we(?:'ll| will) see\b",
        r"let me think about (?:it|that)",
        r"i(?:'ll| will) (?:get back to you|think about it|have to (?:check|see|ask))",
        r"i(?:'d| would) (?:have|need) to (?:check|see|ask|think)",
        r"we(?:'d| would) (?:have|need) to (?:see|check|think about)",
        r"(?:maybe|perhaps) (?:later|another time|down the (?:line|road))",
        r"not (?:right )?now\b",
        r"i(?:'m| am) not sure (?:that(?:'s| is)|if that(?:'s| is)) (?:possible|feasible|realistic|going to work)",
        r"that(?:'s| is) (?:a lot|quite a lot|a big ask)",
        r"(?:no|not) promises",
    ],
}

_WH_OR_AUX_START = re.compile(
    r"^(?:what|why|how|when|where|who|whom|which|is|are|was|were|do|does|did|can|could|"
    r"would|should|will|shall|may|might|have|has|had|isn't|aren't|don't|doesn't|didn't|"
    r"can't|couldn't|wouldn't|shouldn't)\b",
    re.IGNORECASE,
)

_WORD = re.compile(r"[A-Za-z0-9']+")

# Function words removed before Jaccard so that "do we have to X" ~ "so we'd need to X".
_STOPWORDS = frozenset(
    "a an the and or but so if then than that this these those is are was were be been being "
    "am do does did done have has had having can could would should will shall may might must "
    "i me my we us our you your he she it they them their his her its of to in on at by for "
    "with from as into about over under up down out off not no yes yeah ok okay just really "
    "very also too again still even well um uh oh like there here what which who whom when where "
    "why how all any some more most such own same other".split()
)

# Candidate jargon from the wearer's turns.
_ACRONYM = re.compile(r"\b[A-Z][A-Z0-9]{1,7}s?\b")                 # SSO, OAuth2 (partial), K8S
_CAMEL_OR_TECH = re.compile(r"\b(?:[a-z]+[A-Z][A-Za-z0-9]*|[A-Za-z]+(?:[-_/][A-Za-z0-9]+)+|[A-Za-z]+\d+[A-Za-z]*)\b")
_MID_SENTENCE_CAP = re.compile(r"(?<=[a-z,]\s)([A-Z][a-z]{3,})\b")  # "the Gateway", not "Gateway" at start
_CAP_STOPLIST = frozenset(
    "monday tuesday wednesday thursday friday saturday sunday january february march april june "
    "july august september october november december".split()
)

DEFAULT_JARGON_TERMS: tuple[str, ...] = (
    "integration", "replacement", "migration", "middleware", "orchestration", "latency",
    "throughput", "deprecation", "backward compatible", "single sign-on", "data pipeline",
    "schema", "endpoint", "webhook", "tenant", "multi-tenant", "sla", "api", "sdk", "on-prem",
    "sandbox", "rollout", "onboarding", "failover", "idempotent",
)


def _compile(fragments: Iterable[str]) -> re.Pattern[str]:
    joined = "|".join(f"(?:{f.replace('^', _CLAUSE_START, 1) if f.startswith('^') else f})" for f in fragments)
    return re.compile(joined, re.IGNORECASE)


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _WORD.findall(text)]


def _content_tokens(text: str) -> set[str]:
    return {t for t in _tokens(text) if t not in _STOPWORDS and len(t) > 1}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class LinguisticDetector:
    """Stateless per-turn detector. All history-dependent logic reads from `history` only."""

    def __init__(
        self,
        jargon_terms: Iterable[str] = DEFAULT_JARGON_TERMS,
        repeat_threshold: float = 0.5,
        repeat_min_shared: int = 2,
        jargon_min_wearer_uses: int = 2,
        jargon_min_counterpart_turns: int = 2,
    ) -> None:
        self._rx = {name: _compile(frags) for name, frags in PATTERNS.items()}
        self._jargon_list = tuple(sorted({t.lower() for t in jargon_terms}, key=len, reverse=True))
        self._jargon_rx = re.compile(
            r"\b(?:" + "|".join(re.escape(t) for t in self._jargon_list) + r")\b", re.IGNORECASE
        ) if self._jargon_list else None
        self.repeat_threshold = repeat_threshold
        self.repeat_min_shared = repeat_min_shared
        self.jargon_min_wearer_uses = jargon_min_wearer_uses
        self.jargon_min_counterpart_turns = jargon_min_counterpart_turns

    # ------------------------------------------------------------------ public
    def analyze(self, text: str, speaker: Speaker, history: list[Turn]) -> LinguisticSignals:
        t = text.strip()
        if not t:
            return LinguisticSignals(minimal_response=True)

        hit = {name: bool(rx.search(t)) for name, rx in self._rx.items()}
        is_q = self.is_question(t)
        # A hedged agreement is not an explicit one; don't double count.
        explicit_agree = hit["explicit_agreement"] and not hit["hedged_agreement"]
        # "No, I don't follow" is a clarification, not a disagreement.
        explicit_disagree = hit["explicit_disagreement"] and not hit["clarification_request"]

        repeat_of = self._repeat_of(t, speaker, history) if is_q else None
        words = _tokens(t)

        return LinguisticSignals(
            is_question=is_q,
            clarification_request=hit["clarification_request"],
            repeated_question=repeat_of is not None,
            repeat_of_turn_id=repeat_of,
            explicit_agreement=explicit_agree,
            explicit_disagreement=explicit_disagree,
            hedged_agreement=hit["hedged_agreement"],
            soft_refusal=hit["soft_refusal"],
            minimal_response=len(words) <= 3,
            jargon_terms=self._jargon(t, speaker, history, queried=hit["clarification_request"] or is_q),
        )

    @staticmethod
    def is_question(text: str) -> bool:
        t = text.strip()
        if t.endswith("?"):
            return True
        # ASR often drops punctuation: accept a clause-initial wh-/aux word only when the
        # utterance is short enough to plausibly be a single question.
        return bool(_WH_OR_AUX_START.match(t)) and len(_tokens(t)) <= 12

    def wearer_terms(self, text: str) -> list[str]:
        """Candidate jargon in one wearer utterance (acronyms, camelCase, mid-sentence capitals, list)."""
        found: dict[str, None] = {}
        for m in _ACRONYM.finditer(text):
            if m.group(0) not in ("I", "A", "OK"):
                found[m.group(0)] = None
        for m in _CAMEL_OR_TECH.finditer(text):
            found[m.group(0)] = None
        for m in _MID_SENTENCE_CAP.finditer(text):
            if m.group(1).lower() not in _CAP_STOPLIST:
                found[m.group(1)] = None
        if self._jargon_rx:
            for m in self._jargon_rx.finditer(text):
                found[m.group(0).lower()] = None
        return list(found)

    # ----------------------------------------------------------------- private
    def _repeat_of(self, text: str, speaker: Speaker, history: list[Turn]) -> Optional[int]:
        cur = _content_tokens(text)
        if len(cur) < self.repeat_min_shared:
            return None
        best: tuple[float, Optional[int]] = (0.0, None)
        for turn in history:
            if turn.speaker != speaker or not (turn.linguistic.is_question or self.is_question(turn.text)):
                continue
            prev = _content_tokens(turn.text)
            score = jaccard(cur, prev)
            if score >= self.repeat_threshold and len(cur & prev) >= self.repeat_min_shared and score > best[0]:
                best = (score, turn.turn_id)
        return best[1]

    def _jargon(self, text: str, speaker: Speaker, history: list[Turn], queried: bool) -> list[str]:
        """Terms the wearer introduced that the counterpart now queries, or has never echoed."""
        if speaker != "counterpart":
            return []
        uses: dict[str, int] = {}            # term -> number of wearer turns using it
        first_use: dict[str, int] = {}       # term -> index in history of first wearer use
        for i, turn in enumerate(history):
            if turn.speaker != "wearer":
                continue
            for term in self.wearer_terms(turn.text):
                uses[term] = uses.get(term, 0) + 1
                first_use.setdefault(term, i)
        if not uses:
            return []

        cp_texts = [h.text.lower() for h in history if h.speaker == "counterpart"]
        cp_since_first = {
            term: sum(1 for h in history[i:] if h.speaker == "counterpart") + 1  # +1 = this turn
            for term, i in first_use.items()
        }
        low = text.lower()
        asked: list[str] = []
        unechoed: list[str] = []
        for term, n in uses.items():
            word = re.compile(r"\b" + re.escape(term.lower()) + r"\b")
            in_this_turn = bool(word.search(low))
            if queried and in_this_turn:
                asked.append(term)                                 # counterpart asks about it
            elif (
                not in_this_turn
                and n >= self.jargon_min_wearer_uses
                and cp_since_first[term] >= self.jargon_min_counterpart_turns
                and not any(word.search(c) for c in cp_texts)
            ):
                unechoed.append(term)                              # counterpart never echoes it
        # An explicit query is the sharper nomination; don't dilute it with the weaker signal.
        return asked or unechoed
