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
                                       │  Coach (state machine + cue scheduler)   │
                                       │   Nominator  → rules name a candidate     │
                                       │   Interpreter→ Muse Spark weighs it       │
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
| "Insufficient evidence" is a valid answer | `Interpreter` schema has `intervene: false`; `Coach` records the hypothesis and stays silent |
| One well-timed cue beats a constant meter | `Coach`: cues only on completed turns, throttle (`min_seconds_between_cues`), stale cues dropped |
| Cues echo into the mic | `Coach.on_cue_played` marks overlapping turns `coaching_overlap`; acoustic signals on those are ignored |
| The wearer's intent is an input, not an inference | `SessionConfig.goal` + `intended_message` anchor every interpreter prompt |

## State machine

```
LISTENING ─nominated─▶ POSSIBLE_MISMATCH ─interpreter says intervene─▶ REPAIR_SUGGESTED ─▶ AWAITING_RESPONSE
    ▲                        │ silent / throttled / stale                         │ next counterpart turn judged
    └────────────────────────┴──────────────────── RESOLVED | UNRESOLVED ◀─────────┘
```

## Latency budget (targets; measure with `scripts/bench_signals.py` and `scripts/eval_scenarios.py --muse`)

| Stage | Target | Mechanism |
|---|---|---|
| Linguistic rules | < 5 ms | regex table |
| Text emotion | < 100 ms | int8 ONNX RoBERTa on CPU |
| Prosody features | < 50 ms / window | numpy only |
| Speech emotion | async, 1.5 s timeout | never blocks a cue |
| Muse interpret | p50 < 2 s, drop if > 8 s | compact context, JSON schema, one call per nomination |

## Data flywheel

Every session is a JSONL graph in `runs/`. Each intervention row carries the observed signals, the
hypothesis, the strategy, and the judged outcome. That is the training signal for the interesting future
model: *which repair reduces which kind of mismatch*, conditioned on the interaction so far.
