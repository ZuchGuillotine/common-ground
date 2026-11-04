"""Shared data contracts. Every module speaks these types; nothing else crosses module boundaries.

Design rules (see docs/ARCHITECTURE.md):
  * Observed signals, model hypotheses, recommendations, and outcomes are kept separate.
  * Acoustic labels never override linguistic evidence; they are evidence, not verdicts.
  * Everything carries a certainty; "insufficient evidence" is a first-class result.
"""
from __future__ import annotations

from enum import Enum
from typing import Literal, Optional
from pydantic import BaseModel, Field


Speaker = Literal["wearer", "counterpart", "unknown"]
Certainty = Literal["none", "tentative", "moderate", "strong"]


class SessionGoal(str, Enum):
    EXPLAIN = "explain"            # explain something accurately
    UNDERSTAND = "understand"      # understand the other person's concerns
    RESOLVE = "resolve"            # resolve disagreement or tension
    NEGOTIATE = "negotiate"        # reach a mutually acceptable outcome


class SessionConfig(BaseModel):
    session_id: str
    goal: SessionGoal = SessionGoal.EXPLAIN
    intended_message: Optional[str] = None   # "I'm offering an integration, not a replacement"
    min_seconds_between_cues: float = 20.0
    consent_confirmed: bool = False


# ---------------------------------------------------------------- perception
class AcousticFeatures(BaseModel):
    """Per-utterance prosody, normalised against the same speaker's running baseline where possible."""
    duration_s: float
    rms_db: Optional[float] = None
    pitch_hz_mean: Optional[float] = None
    pitch_hz_std: Optional[float] = None
    pitch_delta_vs_baseline: Optional[float] = None   # semitones vs speaker baseline
    energy_delta_vs_baseline: Optional[float] = None  # dB vs speaker baseline
    speech_rate_sps: Optional[float] = None           # syllables / sec (approximate)
    pause_ratio: Optional[float] = None               # fraction of window that is silence
    leading_pause_s: Optional[float] = None           # silence before this utterance began


class AcousticEmotion(BaseModel):
    """Output of a pretrained speech-emotion model. Tentative by construction."""
    model: str
    label: str
    scores: dict[str, float] = Field(default_factory=dict)


class TextEmotion(BaseModel):
    model: str
    top: list[tuple[str, float]] = Field(default_factory=list)   # (label, prob) sorted desc


class LinguisticSignals(BaseModel):
    """Cheap, rule-based, high-precision detectors. These nominate problems; they never diagnose."""
    is_question: bool = False
    clarification_request: bool = False     # "what do you mean by", "so you're saying", "wait, "
    repeated_question: bool = False          # semantically similar to an earlier question
    repeat_of_turn_id: Optional[int] = None
    explicit_agreement: bool = False         # "yeah that makes sense", "sure", "ok"
    explicit_disagreement: bool = False      # "no", "I don't think", "that's not"
    hedged_agreement: bool = False           # "sure, I guess", "I suppose"
    soft_refusal: bool = False               # "that might be difficult", "we'll see"
    jargon_terms: list[str] = Field(default_factory=list)  # terms wearer used that counterpart queried / never used
    minimal_response: bool = False           # <= 3 words
    interruption: bool = False


class Turn(BaseModel):
    turn_id: int
    speaker: Speaker
    text: str
    t_start: float
    t_end: float
    is_final: bool = True
    linguistic: LinguisticSignals = Field(default_factory=LinguisticSignals)
    text_emotion: Optional[TextEmotion] = None
    acoustic: Optional[AcousticFeatures] = None
    acoustic_emotion: Optional[AcousticEmotion] = None
    coaching_overlap: bool = False  # glasses were playing a cue during this turn; audio is suspect


# ------------------------------------------------------------- interpretation
class MismatchType(str, Enum):
    TERM_AMBIGUITY = "term_ambiguity"
    POLITE_CONFUSION = "polite_confusion"       # agreement words + inconsistent follow-up
    UNANSWERED_OBJECTION = "unanswered_objection"
    INDIRECT_REFUSAL = "indirect_refusal"
    ESCALATING_FRICTION = "escalating_friction"
    GENUINE_DISAGREEMENT = "genuine_disagreement"  # understood, disagrees: NOT a comprehension gap
    DISTRACTION = "distraction"
    NONE = "none"


class Hypothesis(BaseModel):
    type: MismatchType
    summary: str
    evidence_turns: list[int] = Field(default_factory=list)
    certainty: Certainty = "tentative"
    alternatives: list[str] = Field(default_factory=list)  # competing explanations, kept visible


class Strategy(str, Enum):
    CLARIFY_DEFINITION = "clarify_definition"
    ASK_CHECK_QUESTION = "ask_check_question"     # "did I explain that distinction clearly?"
    LABEL_EMOTION = "label_emotion"               # Voss: "it sounds like ..."
    MIRROR = "mirror"                             # repeat last 1-3 words as a question
    SLOW_DOWN = "slow_down"
    SWITCH_FRAME = "switch_frame"                 # analogy / example instead of more explanation
    ACKNOWLEDGE_OBJECTION = "acknowledge_objection"
    INVITE_NO = "invite_no"                       # "would it be ridiculous to..."
    STAY_SILENT = "stay_silent"


class Intervention(BaseModel):
    strategy: Strategy
    suggestion: str                       # phone screen: one sentence
    earpiece_cue: str                     # glasses speaker: <= 4 words
    success_test: str                     # what the next counterpart turn should show
    rationale: str = ""


Outcome = Literal["pending", "resolved", "unresolved", "superseded", "no_intervention"]


class CoachState(str, Enum):
    LISTENING = "LISTENING"
    POSSIBLE_MISMATCH = "POSSIBLE_MISMATCH"
    REPAIR_SUGGESTED = "REPAIR_SUGGESTED"
    AWAITING_RESPONSE = "AWAITING_RESPONSE"
    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"


class GraphEvent(BaseModel):
    """One node of the conversation graph: observation, hypothesis, recommendation, outcome kept apart."""
    turn_id: int
    speaker: Speaker
    transcript: str
    signals: dict = Field(default_factory=dict)
    interpretation: Optional[Hypothesis] = None
    intervention: Optional[Intervention] = None
    outcome: Outcome = "no_intervention"
    state_after: CoachState = CoachState.LISTENING


# ------------------------------------------------------------------ transport
class ClientMessage(BaseModel):
    """Phone -> server. Exactly one of the payload fields is set."""
    type: Literal["start", "audio", "text_turn", "cue_played", "assign_speaker", "stop"]
    config: Optional[SessionConfig] = None
    # audio: base64 PCM16 mono 16 kHz, sent as binary frames in practice; this field is for tests
    pcm16_b64: Optional[str] = None
    text_turn: Optional[dict] = None     # {"speaker": ..., "text": ..., "t_start":..., "t_end":...}
    cue_window: Optional[tuple[float, float]] = None
    speaker_map: Optional[dict[str, Speaker]] = None   # {"S0": "wearer", "S1": "counterpart"}


class ServerMessage(BaseModel):
    """Server -> phone."""
    # cue: {tier: 0|1, turn_id, earpiece_cue, suggestion, strategy, success_test, hypothesis}; a Tier 1 cue may
    # arrive with an empty suggestion, completed by a cue_detail message once the model's stream finishes.
    type: Literal["transcript", "turn", "state", "cue", "cue_detail", "graph_event", "error", "metrics"]
    payload: dict = Field(default_factory=dict)
    t: float
