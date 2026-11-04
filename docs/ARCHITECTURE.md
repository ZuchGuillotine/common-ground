# Architecture

Common Ground is a closed-loop conversational repair system. It does not try to read emotions off
a face; it looks for observable evidence that two people have stopped meaning the same thing, proposes
one concrete repair to the wearer, and then checks whether the next turn shows the repair worked.

```
Ray-Ban Meta glasses ──mic PCM 16k──▶ iOS app ──WebSocket──▶ FastAPI server
        ▲ earpiece cue (TTS)            │ phone UI            │
        └───────────────────────────────┘                     ▼
                                       ┌──────────────────────────────────────────┐
                                       │  Pipeline                                │
                                       │   Muse Voice Transcribe (diarized turns)  │
                                       │   LinguisticDetector   (rules, <5 ms)     │
                                       │   TextEmotion (RoBERTa GoEmotions, ONNX)  │
                                       │   AcousticExtractor (numpy prosody)       │
                                       │   SpeechEmotion (wav2vec2, optional)      │
                                       └──────────────┬───────────────────────────┘
                                                      ▼  annotated Turn
                                       ┌──────────────────────────────────────────┐
                                       │  Coach (state machine + two-tier cues)   │
                                       │   Nominator  → rules name a candidate     │
                                       │   Tier 0     → fixed-vocabulary cue, now  │
                                       │   Evidence   → accumulate; worth a call?  │
                                       │   Tier 1     → Muse Spark, streamed, in   │
                                       │                background, next window    │
                                       │   Graph      → observation / hypothesis / │
                                       │                recommendation / outcome   │
                                       └──────────────────────────────────────────┘
```

## Principles encoded in code, not prose

| Principle | Where |
|---|---|
| Observed signals, hypotheses, recommendations and outcomes are separate fields | `schema.GraphEvent` |
| Acoustic labels are evidence, never verdicts | `graph.compact_transcript` renders them as weak hints; `Nominator` only uses prosody *with* a linguistic signal |
| Disagreement ≠ misunderstanding | `MismatchType.GENUINE_DISAGREEMENT` and `fallback.TEMPLATES` route it to acknowledgement, never re-explanation |
| "Insufficient evidence" is a valid answer | `Interpreter` schema has `act: false`; `Coach` records the hypothesis and stays silent |
| One well-timed cue beats a constant meter | `Coach`: cues only at turn boundaries, throttle (`min_seconds_between_cues`), stale cues dropped |
| Minimal always, reasoned occasionally | Tier 0 is a six-word vocabulary (`CUE_VOCABULARY`) delivered within ~1 s; Tier 1 (Muse) is spent only when `EvidenceAccumulator` says the pattern sustains, a cue failed, or a single nomination is strong |
| The model never blocks the earpiece | Tier 1 runs as a background task, streams, fires on the first three fields, is held while anyone speaks and dropped if stale; outcome judging waits at most 50 ms inline |
| Cues echo into the mic | `Coach.on_cue_played` marks overlapping turns `coaching_overlap`; acoustic signals on those are ignored |
| The wearer's intent is an input, not an inference | `SessionConfig.goal` + `intended_message` anchor every interpreter prompt |

## State machine

```
LISTENING ─nominated─▶ POSSIBLE_MISMATCH ─Tier 0 cue (and later, maybe, a Tier 1 cue)─▶ REPAIR_SUGGESTED ─▶ AWAITING_RESPONSE
    ▲                        │ silent / throttled / stale                         │ next counterpart turn judged
    └────────────────────────┴──────────────────── RESOLVED | UNRESOLVED ◀─────────┘
```

## Latency budget (measured 2026-11-04, see docs/RESULTS.md; `scripts/eval_scenarios.py --muse --realtime --partials 2`)

| Stage | Measured | Mechanism |
|---|---|---|
| Voice Transcribe final text | ~0.6 s after speech offset | endpointer needs ~500 ms of silence; partials trail audio by < 1 s |
| Linguistic rules | < 5 ms | regex table |
| Text emotion | < 100 ms | int8 ONNX RoBERTa on CPU |
| Prosody features | < 50 ms / window | numpy only |
| Speech emotion | async, 1.5 s timeout | never blocks a cue |
| **Tier 0 cue** | **~0.7 s after speech offset** | fixed vocabulary, no model |
| Tier 1 Muse call | ~2 s to first byte + hidden reasoning + ~1 s visible JSON (p50 ~5 s) | `reasoning_effort=minimal`, 7 short fields, early fields (`act`,`cue`,`how`) sort first |
| Tier 1 cue, speculative | starts on the counterpart's partial; can be ready before the turn ends | kept only if the final transcript confirms the same nomination |
| Tier 1 delivery | next listening window; dropped after 8 s or two counterpart turns | the wearer is never interrupted mid-sentence |

### Why two tiers

Muse Spark cannot answer in under ~4 s: it reasons before every token (`reasoning_effort: none` is
rejected) and the first byte alone takes ~2 s. Seven seconds is an eternity mid-conversation, but the
wearer does not need coaching throughout; they need to know *that* a moment happened at once, and
*what to do about it* only occasionally. So the deterministic layer speaks first from a six-word
vocabulary, and the reasoning layer is reserved for sustained patterns, where its sentence arrives at
the next pause rather than interrupting.

## Data flywheel

Every session is a JSONL graph in `runs/`. Each intervention row carries the observed signals, the
hypothesis, the strategy, and the judged outcome. That is the training signal for the interesting future
model: *which repair reduces which kind of mismatch*, conditioned on the interaction so far.
