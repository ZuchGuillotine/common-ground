"""Muse Spark as the Tier 1 interpreter: weighs a nominated mismatch and writes one short repair.

Called only when the EvidenceAccumulator decides a nomination is worth 4-7 s of model time, or to
judge the outcome of a delivered cue (off the hot path). It receives compact state, must weigh
competing explanations, and is explicitly allowed to answer "insufficient evidence" / stay silent.

Latency design (measured live, see docs/RESULTS.md):
  * The call is streamed. Muse emits the JSON keys in ALPHABETICAL order (verified; schema order is
    ignored), so the three fields the earpiece needs are named `act`, `cue`, `how` and sort ahead of
    `say`, `sure`, `test`, `type`. `parse_early()` fires the moment those three have streamed, roughly
    a second before the full object completes.
  * Output is small: a 4-word cue, a <=12-word suggestion, a one-line success test. No rationale,
    summary or alternatives on the hot path; the nomination already carries those.
  * Context is the last six turns only.
"""
from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Protocol

from ..schema import (Certainty, GraphEvent, Hypothesis, Intervention, MismatchType, Outcome,
                      SessionConfig, Strategy)
from .graph import ConversationGraph
from .nominator import Nomination


class JsonChat(Protocol):
    async def complete_json(self, messages: list[dict[str, Any]], *, json_schema: Optional[dict] = None,
                            max_tokens: int = 400, temperature: float = 0.2) -> dict: ...


@dataclass
class EarlyCue:
    """The first three streamed fields: enough to speak the earpiece cue."""
    intervene: bool
    strategy: Strategy
    earpiece_cue: str


OnEarly = Callable[[EarlyCue], Optional[Awaitable[None]]]

SYSTEM = """You are the interpretation layer of Common Ground, a communication copilot worn by one
speaker (the "wearer") talking with a "counterpart". You never claim to read minds. You reason
about OBSERVABLE evidence of failed understanding, misaligned meaning, or friction, and prefer
helping the wearer ASK a good question over asserting what the counterpart feels. Disagreement is
not misunderstanding. Voice/tone signals are weak evidence and never override what was said. If
evidence is weak or the conversation is going fine, do not act. The wearer is mid-conversation: a
cue is at most 4 words, a suggestion at most 12, sayable in the next breath, serving mutual
understanding and voluntary agreement, never manipulation."""

# Key names are deliberately short and chosen so that the fields the earpiece needs sort first
# alphabetically (act < cue < how < say < sure < test < type). See module docstring.
INTERPRET_SCHEMA = {
    "type": "object",
    "properties": {
        "act": {"type": "boolean", "description": "cue the wearer now?"},
        "cue": {"type": "string", "description": "earpiece cue, at most 4 words; empty if act is false"},
        "how": {"type": "string", "enum": [s.value for s in Strategy], "description": "strategy; stay_silent if act is false"},
        "say": {"type": "string", "description": "one sentence, at most 12 words, the wearer could say or do next"},
        "sure": {"type": "string", "enum": ["none", "tentative", "moderate", "strong"], "description": "certainty"},
        "test": {"type": "string", "description": "what the counterpart's next turn shows if the repair worked"},
        "type": {"type": "string", "enum": [m.value for m in MismatchType], "description": "best-supported explanation"},
    },
    "required": ["act", "cue", "how", "say", "sure", "test", "type"],
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

CONTEXT_TURNS = 6
INTERPRET_MAX_TOKENS = 256     # visible output; the Muse client adds reasoning headroom on top

# Matches the streamed prefix once act, cue and how are complete (the trailing comma guarantees it).
_EARLY_RE = re.compile(
    r'^\s*\{\s*"act"\s*:\s*(true|false)\s*,\s*"cue"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*"how"\s*:\s*"([a-z_]+)"\s*,')


def parse_early(prefix: str) -> Optional[EarlyCue]:
    m = _EARLY_RE.match(prefix)
    if not m:
        return None
    cue = m.group(2).encode().decode("unicode_escape") if "\\" in m.group(2) else m.group(2)
    return EarlyCue(intervene=m.group(1) == "true", strategy=_strategy(m.group(3)), earpiece_cue=_cap_words(cue, 4))


def _goal_line(cfg: SessionConfig) -> str:
    s = f"Wearer's goal: {cfg.goal.value}."
    if cfg.intended_message:
        s += f' Intended message: "{cfg.intended_message}".'
    return s


class Interpreter:
    def __init__(self, chat: JsonChat):
        self.chat = chat

    def interpret_messages(self, cfg: SessionConfig, graph: ConversationGraph, nom: Nomination) -> list[dict]:
        user = f"""{_goal_line(cfg)}

Recent conversation (braces = rule detections; [voice~x] = weak acoustic estimate):
{graph.compact_transcript(CONTEXT_TURNS)}

Rules nominated turn #{graph.turns[-1].turn_id}: {nom.type.value} ({nom.reason}).
Competing explanations: {', '.join(a.value for a in nom.alternatives) or 'none'}.
Usual strategy: {STRATEGY_HINTS.get(nom.type, 'stay_silent')}.

Decide whether to cue the wearer right now (act), the cue (<=4 words), the strategy (how),
one thing to say or do next (say, <=12 words), your certainty (sure), the success test (test)
and the best-supported explanation (type). If act is false: cue empty, how stay_silent."""
        return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]

    async def interpret(self, cfg: SessionConfig, graph: ConversationGraph, nom: Nomination, *,
                        on_early: Optional[OnEarly] = None) -> tuple[Hypothesis, Optional[Intervention]]:
        messages = self.interpret_messages(cfg, graph, nom)
        stream = getattr(self.chat, "stream", None)
        if stream is not None:
            buf, fired = "", False
            async for delta in stream(messages, json_schema=INTERPRET_SCHEMA, max_tokens=INTERPRET_MAX_TOKENS):
                buf += delta
                if not fired and on_early is not None:
                    early = parse_early(buf)
                    if early is not None:
                        fired = True
                        res = on_early(early)
                        if inspect.isawaitable(res):
                            await res
            from ..muse.chat import extract_json   # local import: engine must not depend on httpx at import time
            out = extract_json(buf)
        else:
            out = await self.chat.complete_json(messages, json_schema=INTERPRET_SCHEMA, max_tokens=INTERPRET_MAX_TOKENS)
        return self.to_result(out, nom)

    @staticmethod
    def to_result(out: dict, nom: Nomination) -> tuple[Hypothesis, Optional[Intervention]]:
        typ = out.get("type", nom.type.value)
        hyp = Hypothesis(
            type=MismatchType(typ) if typ in MismatchType._value2member_map_ else nom.type,
            summary=nom.reason,
            evidence_turns=list(nom.evidence_turns),
            certainty=_cert(out.get("sure")),
            alternatives=[a.value for a in nom.alternatives],
        )
        if not out.get("act") or out.get("how") == Strategy.STAY_SILENT.value or hyp.certainty == "none":
            return hyp, None
        return hyp, Intervention(
            strategy=_strategy(out.get("how")),
            suggestion=_cap_words(str(out.get("say", "")).strip(), 16),
            earpiece_cue=_cap_words(str(out.get("cue", "")), 4),
            success_test=str(out.get("test", "")),
        )

    async def judge_outcome(self, cfg: SessionConfig, graph: ConversationGraph, pending: GraphEvent) -> tuple[Outcome, str]:
        iv = pending.intervention
        assert iv is not None
        user = f"""{_goal_line(cfg)}

Earlier, on turn #{pending.turn_id}, the wearer was advised: "{iv.suggestion or iv.earpiece_cue}"
Success test: {iv.success_test}

Conversation since then:
{graph.compact_transcript(4)}

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


def _cap_words(s: str, n: int) -> str:
    return " ".join(s.split()[:n])
