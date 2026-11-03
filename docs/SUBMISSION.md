# Submission draft: Common Ground

*Real-time AI coaching for the moments when words stop meaning the same thing.*

## The problem
Two people speaking the same language still fail to communicate: a term carries different connotations,
someone says "makes sense" while holding a different mental model, a soft "that might be difficult" is a
refusal nobody names, an explanation gets repeated louder instead of reframed. Humans are poor at noticing
this in the moment, and existing "emotion AI" answers the wrong question: it labels feelings instead of
detecting whether understanding actually happened.

## What we built
A wearable communication copilot for Ray-Ban Meta glasses. It:
1. **Perceives** a live two-person conversation (Muse Voice Transcribe for diarized turns; local rule, text-emotion
   and prosody detectors for sub-50 ms signals).
2. **Interprets** nominated breakdowns with Muse Spark 1.3, which must weigh competing explanations, declare its
   certainty, and is allowed to answer "stay silent".
3. **Intervenes** with one short earpiece cue (≤ 4 words) and a one-sentence suggestion on the phone, at most
   once every 20 s and only at natural pauses.
4. **Verifies**: the next counterpart turn is judged against an explicit success test, and the outcome is written
   back into a conversation graph as (observation → hypothesis → repair → outcome).

The last step is the point. The system is not an emotion meter; it is a closed loop that measures whether its
own advice worked, and that loop produces the training data for the model we actually want next: which
repair reduces which kind of mismatch.

## Why this is hard, and how we handled it
- **Disagreement is not misunderstanding.** A dedicated hypothesis type routes clear disagreement to acknowledgement,
  never to re-explanation. The evaluation penalises any confusion label on a disagreement scenario.
- **Facial and vocal cues are weak evidence.** Prosody deltas are computed against the same speaker's own baseline
  and are only ever combined with a linguistic signal; they never trigger a cue alone.
- **Latency.** The Muse call is the only network hop on the cue path, runs once per nomination with compact
  context and a JSON schema, and a cue older than 8 s is dropped rather than delivered late.
- **Cue echo.** TTS cues play through the glasses and re-enter the mic; cue windows are reported back and
  overlapping turns are flagged so their acoustic features are ignored.
- **Consent.** A session cannot start without a consent flag; raw audio is never stored; Muse runs in Standard mode.

## Results
See docs/RESULTS.md (filled from `runs/eval_*.json`).

| Metric | Rules only | Rules + Muse |
|---|---|---|
| False intervention rate (neutral scenarios) | 0.00 | _TBD_ |
| Seeded-issue detection rate (hand-authored) | 1.00 | _TBD_ |
| Hypothesis-type accuracy | 1.00 | _TBD_ |
| Held-out (Muse-generated) detection / false-intervention | _TBD_ | _TBD_ |
| Interpreter latency p50 / p95 | 0 ms (templates) | _TBD_ |
| Signal latency per turn (CPU) | ~10 ms | ~10 ms |

Hand-authored fixtures were used to tune the rule detectors, so the held-out generated set is the honest number.

## Limitations
Small, scripted evaluation; no human-outcome study yet; acted/scripted speech is not spontaneous speech; speaker
diarization errors propagate; the glasses' synchronized mic stream is experimental and not publishable to
production; no camera/facial channel (deliberately).

## Links
Repo: https://github.com/ZuchGuillotine/common-ground · Demo video: _TBD_
