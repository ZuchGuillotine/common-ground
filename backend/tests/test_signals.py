"""Perception-tier tests: linguistic rules, text emotion (ONNX), acoustic prosody."""
from __future__ import annotations

import time

import numpy as np
import pytest

from common_ground.schema import Turn
from common_ground.signals import AcousticExtractor, LinguisticDetector, TextEmotionClassifier


# ------------------------------------------------------------------- helpers
def turn(turn_id: int, speaker: str, text: str) -> Turn:
    return Turn(turn_id=turn_id, speaker=speaker, text=text, t_start=float(turn_id), t_end=turn_id + 1.0)


def vowel(f0: float, dur: float = 3.0, sr: int = 16000, amp: float = 0.3, syl_hz: float = 4.0) -> np.ndarray:
    """Harmonic 'vowel' with syllable-like 4 Hz amplitude modulation, as PCM16."""
    t = np.arange(int(dur * sr)) / sr
    y = sum((1.0 / k) * np.sin(2 * np.pi * f0 * k * t) for k in range(1, 8))
    env = 0.55 + 0.45 * np.sin(2 * np.pi * syl_hz * t - np.pi / 2)
    y = amp * y / np.abs(y).max() * env
    return (y * 32767).astype(np.int16)


# ---------------------------------------------------------------- linguistic
@pytest.fixture(scope="module")
def ling() -> LinguisticDetector:
    return LinguisticDetector()


@pytest.mark.parametrize(
    "text, expected_true, expected_false",
    [
        (
            "Yeah, that makes sense. So would we have to replace everything?",
            {"explicit_agreement", "is_question", "clarification_request"},
            {"hedged_agreement", "soft_refusal", "explicit_disagreement"},
        ),
        ("Sure, I guess.", {"hedged_agreement", "minimal_response"}, {"explicit_agreement"}),
        ("That might be difficult.", {"soft_refusal"}, {"explicit_disagreement", "explicit_agreement"}),
        ("I'll get back to you on that.", {"soft_refusal"}, set()),
        ("Wait, what do you mean by middleware?", {"is_question", "clarification_request"}, {"explicit_disagreement"}),
        ("I'm not sure I follow.", {"clarification_request"}, {"explicit_disagreement", "soft_refusal"}),
        ("No, I don't think that's right.", {"explicit_disagreement"}, {"explicit_agreement", "clarification_request"}),
        ("Ok.", {"explicit_agreement", "minimal_response"}, {"hedged_agreement"}),
        ("How does that work with our current setup", {"is_question", "clarification_request"}, set()),
        ("We shipped it on Monday and everyone liked the new dashboard.", set(), {"is_question", "soft_refusal"}),
    ],
)
def test_linguistic_cases(ling: LinguisticDetector, text: str, expected_true: set[str], expected_false: set[str]):
    s = ling.analyze(text, "counterpart", []).model_dump()
    for k in expected_true:
        assert s[k] is True, f"{text!r}: expected {k}=True, got {s}"
    for k in expected_false:
        assert s[k] is False, f"{text!r}: expected {k}=False, got {s}"


def test_repeated_question(ling: LinguisticDetector):
    history = [
        turn(1, "wearer", "The SSO integration sits in front of your Gateway."),
        turn(2, "counterpart", "Do we have to replace everything?"),
        turn(3, "wearer", "No, it is additive."),
    ]
    history[1].linguistic = ling.analyze(history[1].text, "counterpart", history[:1])
    s = ling.analyze("So we'd need to replace everything then?", "counterpart", history)
    assert s.is_question and s.repeated_question and s.repeat_of_turn_id == 2
    # a different question from the same speaker is not a repeat
    s2 = ling.analyze("When would the rollout start?", "counterpart", history)
    assert not s2.repeated_question and s2.repeat_of_turn_id is None
    # the wearer asking the same words is not the counterpart repeating
    s3 = ling.analyze("Do we have to replace everything?", "wearer", history)
    assert not s3.repeated_question


def test_jargon_queried_and_unechoed(ling: LinguisticDetector):
    history = [
        turn(1, "wearer", "Our SSO integration sits behind the Gateway."),
        turn(2, "counterpart", "Right."),
        turn(3, "wearer", "The SSO integration is additive and the Gateway stays."),
        turn(4, "counterpart", "Okay, sure."),
    ]
    queried = ling.analyze("What's SSO?", "counterpart", history)
    assert queried.jargon_terms == ["SSO"]
    unechoed = ling.analyze("Right, so the front end stays.", "counterpart", history)
    assert {"SSO", "Gateway", "integration"} <= set(unechoed.jargon_terms)
    assert ling.analyze("The SSO integration is additive.", "wearer", history).jargon_terms == []


def test_linguistic_latency(ling: LinguisticDetector):
    history = [turn(i, "counterpart" if i % 2 else "wearer", f"Do we need the {w} layer at all?") for i, w in
               enumerate(["SSO", "Gateway", "schema", "endpoint", "webhook", "tenant"] * 5)]
    text = "Yeah, that makes sense. So would we have to replace everything?"
    t0 = time.perf_counter()
    for _ in range(50):
        ling.analyze(text, "counterpart", history)
    per_turn_ms = (time.perf_counter() - t0) * 1000 / 50
    print(f"\nlinguistic: {per_turn_ms:.3f} ms/turn with {len(history)} history turns")
    assert per_turn_ms < 100


# -------------------------------------------------------------- text emotion
@pytest.fixture(scope="module")
def text_emotion() -> TextEmotionClassifier:
    clf = TextEmotionClassifier()
    ms = clf.warmup()
    print(f"\ntext emotion: load + warmup {ms:.0f} ms")
    return clf


def test_text_emotion_labels_and_latency(text_emotion: TextEmotionClassifier):
    cases = {
        "Yeah, that makes sense.": {"approval", "neutral", "realization", "optimism"},
        "I'm not sure I follow, what do you mean?": {"confusion", "curiosity"},
        "That might be difficult.": {"neutral", "confusion", "disapproval", "nervousness"},
    }
    worst = 0.0
    for text, plausible in cases.items():
        t0 = time.perf_counter()
        r = text_emotion.classify(text)
        ms = (time.perf_counter() - t0) * 1000
        worst = max(worst, ms)
        print(f"text emotion: {ms:6.1f} ms  {text!r} -> {r.top}")
        assert r.model and len(r.top) == 3
        assert all(0.0 <= p <= 1.0 for _, p in r.top)
        assert r.top[0][0] in plausible, r.top
    assert worst < 300, f"slowest classify {worst:.0f} ms"


def test_text_emotion_empty(text_emotion: TextEmotionClassifier):
    assert text_emotion.classify("   ").top == []


# ------------------------------------------------------------------ acoustic
def test_pitch_delta_positive_and_baseline():
    ex = AcousticExtractor()
    a = ex.features(vowel(200), sr=16000, speaker="cp")
    assert a.pitch_hz_mean == pytest.approx(200, abs=5)
    assert a.pitch_delta_vs_baseline is None          # nothing to compare against yet
    assert a.pause_ratio == pytest.approx(0.0, abs=0.05)
    assert a.rms_db is not None and -30 < a.rms_db < 0
    b = ex.features(vowel(300, amp=0.5), sr=16000, speaker="cp")
    assert b.pitch_hz_mean == pytest.approx(300, abs=5)
    assert b.pitch_delta_vs_baseline == pytest.approx(7.02, abs=0.5)   # 12*log2(300/200)
    assert b.energy_delta_vs_baseline is not None and b.energy_delta_vs_baseline > 2
    # a different speaker has an independent baseline
    c = ex.features(vowel(300), sr=16000, speaker="wearer")
    assert c.pitch_delta_vs_baseline is None


def test_silence_and_pauses():
    ex = AcousticExtractor()
    s = ex.features(np.zeros(32000, dtype=np.int16), speaker="cp")
    assert s.pause_ratio == pytest.approx(1.0)
    assert s.pitch_hz_mean is None and s.rms_db is None and s.speech_rate_sps is None
    assert s.duration_s == pytest.approx(2.0)
    gapped = np.concatenate([np.zeros(8000, dtype=np.int16), vowel(150, 1.0), np.zeros(16000, dtype=np.int16), vowel(150, 1.0)])
    g = ex.features(gapped, speaker="cp")
    assert 0.4 < g.pause_ratio < 0.6
    assert g.leading_pause_s == pytest.approx(0.5, abs=0.05)
    assert g.speech_rate_sps == pytest.approx(4.0, abs=1.0)
    assert ex.features(b"", speaker="cp").pause_ratio == 1.0


def test_acoustic_accepts_bytes_and_is_fast():
    ex = AcousticExtractor()
    x = vowel(180, 4.0)
    assert ex.features(x.tobytes(), speaker="w").pitch_hz_mean == pytest.approx(180, abs=5)
    times = []
    for _ in range(20):
        t0 = time.perf_counter()
        ex.features(x, speaker="w")
        times.append((time.perf_counter() - t0) * 1000)
    print(f"\nacoustic: 4 s window median {np.median(times):.1f} ms, max {max(times):.1f} ms")
    assert np.median(times) < 50
