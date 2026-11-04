# Results: Meta Muse integration verified live (2026-11-03)

Everything below was run against the live Meta Model API with a real key from this repository's code
(`backend/common_ground/muse/`, `backend/scripts/`). Total Muse spend for the whole session: **≈ $0.55**
(see [Cost](#cost)); budget was $5.

## What was verified live

| Capability | Call | Result |
|---|---|---|
| Auth + model catalogue | `GET /v1/models` | 200; `{"object":"list","data":[{"id":...,"object":"model","created":0,"owned_by":"meta"}]}`; 9 models incl. `muse-spark-1.3`, `muse-voice-transcribe-1.0` |
| Chat completion | `POST /v1/chat/completions`, `muse-spark-1.3` | 200; `choices[0].message.content` string; `usage` incl. `completion_tokens_details.reasoning_tokens` |
| Structured output | `response_format: {type: json_schema, json_schema: {name, schema}}` | 200; content is compact JSON matching the schema, e.g. `{"answer":"Paris","confidence":1}`; `name` is **required** (400 without it) |
| Reasoning budget | `reasoning_effort` minimal / low / medium | reasoning tokens count against `max_completion_tokens`; exhausted budget → `finish_reason: length`, `content: null` (see corrections) |
| Voice Transcribe handshake | `wss://api.meta.ai/v1/asr/realtime`, key in first text frame | ack `{"sessionId":"voyager/duplex/<32 hex>"}` in ~0.8 s; no HTTP auth header needed |
| Diarization | `mode: DIARIZATION`, 16 kHz PCM, paced | 6/6 turns segmented, labels `A`/`B` alternate correctly with the two voices; all words correct |
| Endpointing | `mode: ENDPOINTING` | 6/6 turns in two runs; one run merged two short turns across a 0.7 s pause into one segment |
| Event types seen | | `speechStart`, `transcript` (partial), `speaker`, `speechEnd`, `speechComplete`, `audioProgress`, and an end-of-stream `transcript{final:true,transcript:""}`; close 1000 "No more transcript segments" |
| Event order per turn | | `speechStart` → partials → `speaker` → `speechEnd` → `speechComplete` (last three in one burst), every turn |
| Timing semantics | | `speechStart`/`speechEnd.audioProcessedMs` are true speech onset/offset; the endpointer closes a turn ~500 ms after the offset |

Test clip: `fixtures/audio/synthetic_two_speakers.wav`, 23.7 s, 16 kHz mono 16-bit, six dialogue lines
rendered with two piper-tts voices (`en_US-lessac-medium` for the wearer, `en_GB-alan-medium` for the
counterpart), the `polite_confusion_01` script. espeak-ng was also installed but piper's voices were used.

### Voice Transcribe latency (paced real-time replay, 80 ms frames)

| Mode | last audio byte → last `speechComplete` | last speech offset → last `speechComplete` | notes |
|---|---|---|---|
| DIARIZATION (3 runs) | +147 ms, +166 ms, +188 ms (raw dump: +170 ms) | 610 ms | ~500 ms of that is the endpointer's trailing-silence window |
| ENDPOINTING (3 runs) | −445 ms, −407 ms (final turn closed before the trailing silence finished streaming) | 494 ms | |

Partials trail the audio by well under a second (roughly one 160–400 ms chunk).

Transcript quality on the clip: DIARIZATION got every word in all three runs ("roll out"/"Rollout" vary);
ENDPOINTING produced "Rolal" for "rollout" in two of three runs. `speechComplete` text was capitalised and
punctuated in four of five sessions and lower-case/unpunctuated in one.

## Evaluation: rules-only vs Muse Spark interpreter

`python scripts/eval_scenarios.py [--muse] [--dir ...]`. Both interpreters receive the same rule nominations;
"rules-only" uses the template `FallbackInterpreter`. Muse runs use `muse-spark-1.3`,
`reasoning_effort=minimal` (the new default) unless stated. Interpreter latency is wall-clock per call.
Fixtures were not edited.

### Hand-authored set (`fixtures/scenarios`, 14 scenarios)

| metric | rules-only | Muse (minimal) | Muse (low, first run) |
|---|---|---|---|
| false_intervention_rate (3 neutral) | 0.0 | 0.0 | 0.0 |
| seeded_detection_rate (8 seeded) | 1.0 | 0.875 | 0.75 |
| type_accuracy | 1.0 | 1.0 | 1.0 |
| scenarios_with_failures | 0 | 1 | 2 |
| interpret p50 / p95 (s) | 0.0 / 0.0 | 5.51 / 6.45 | not measured (see note) |
| Muse calls / tokens in / out | – | 13 / 7,962 / 5,570 | 13 / 7,996 / 16,604 |
| Muse cost (USD) | – | 0.0336 | 0.0806 |

`runs/eval_rules.json`:

```
OK escalation_01            unanswered_objection / acknowledge_objection @#3: "Answer their question"
OK escalation_02            genuine_disagreement / acknowledge_objection @#1: "They disagree. Acknowledge."
OK genuine_disagreement_01  silent
OK genuine_disagreement_02  genuine_disagreement / acknowledge_objection @#1: "They disagree. Acknowledge."
OK indirect_refusal_01      indirect_refusal / label_emotion @#1: "Name the hesitation"
OK indirect_refusal_02      indirect_refusal / label_emotion @#1: "Name the hesitation"
OK jargon_01                term_ambiguity / clarify_definition @#1: "Define the term"
OK jargon_02                term_ambiguity / clarify_definition @#1: "Define the term"
OK neutral_01               silent
OK neutral_02               silent
OK neutral_03               silent
OK polite_confusion_01      polite_confusion / ask_check_question @#3: "Ask them to restate"
OK polite_confusion_02      polite_confusion / ask_check_question @#3: "Ask them to restate"
OK same_words_01            silent

{"interpreter": "fallback", "n": 14, "false_intervention_rate": 0.0, "seeded_detection_rate": 1.0,
 "type_accuracy": 1.0, "scenarios_with_failures": 0, "interpret_calls": 10,
 "interpret_p50_s": 0.0, "interpret_p95_s": 0.0}
```

`runs/eval_muse.json` (reasoning_effort=minimal):

```
OK escalation_01            unanswered_objection / acknowledge_objection @#3: "Acknowledge then detail"
OK escalation_02            genuine_disagreement / acknowledge_objection @#1: "Acknowledge, invite reason"
OK genuine_disagreement_01  silent
OK genuine_disagreement_02  genuine_disagreement / acknowledge_objection @#1: "Acknowledge pilot promise"
OK indirect_refusal_01      indirect_refusal / invite_no @#1: "Invite a no"
OK indirect_refusal_02      indirect_refusal / invite_no @#1: "Invite a no"
OK jargon_01                term_ambiguity / clarify_definition @#1: "Define retention policy"
OK jargon_02                term_ambiguity / clarify_definition @#1: "Define escrow simply"
OK neutral_01               silent
OK neutral_02               silent
OK neutral_03               silent
OK polite_confusion_01      polite_confusion / ask_check_question @#3: "Check their understanding"
XX polite_confusion_02      silent   <- missed_seeded_issue
OK same_words_01            silent

{"interpreter": "muse", "n": 14, "false_intervention_rate": 0.0, "seeded_detection_rate": 0.875,
 "type_accuracy": 1.0, "scenarios_with_failures": 1, "interpret_calls": 10,
 "interpret_p50_s": 5.51, "interpret_p95_s": 6.446,
 "muse_usage": {"calls": 13, "input_tokens": 7962, "output_tokens": 5570, "cost_usd": 0.03363}}
```

Earlier Muse runs on this set, for the record: with `reasoning_effort=low` the first run missed
`indirect_refusal_02` and `polite_confusion_02` (detection 0.75, $0.0806); the second `low` run **crashed**
when one interpret call spent all 2,558 budget tokens on reasoning and returned `content: null`. That crash
is what motivated the `minimal` default and the fallback retry described under corrections.

### Muse-generated held-out set (`fixtures/scenarios/generated`, 12 scenarios, 2 per type)

Generated by `python scripts/gen_scenarios.py --per-type 2` (12 calls, 1,781 in / 9,710 out tokens, $0.0435).
Evaluated with the new `--dir` flag.

| metric | rules-only | Muse (minimal) | Muse (low, run 1 / run 2) |
|---|---|---|---|
| false_intervention_rate (2 neutral) | 0.0 | 0.0 | 0.0 / 0.0 |
| seeded_detection_rate (6 seeded) | 1.0 | 1.0 | 1.0 / 1.0 |
| type_accuracy | 0.8 | 0.9 | 0.8 / 0.778 |
| scenarios_with_failures | 2 | 1 | 2 / 2 |
| interpret p50 / p95 (s) | 0.0 / 0.0 | 4.43 / 6.94 | n/a / 13.68 / 21.64 |
| Muse calls / tokens in / out | – | 22 / 13,419 / 10,467 | 20 / 12,252 / 26,396 and 21 / 12,966 / 27,573 |
| Muse cost (USD) | – | 0.0613 | 0.1275 / 0.1334 |

`runs/eval_rules_generated.json`:

```
XX gen_escalating_friction_00   term_ambiguity / clarify_definition @#1: "Define the term"   <- wrong_turn:1 not in [3, 5], wrong_type:term_ambiguity
XX gen_escalating_friction_01   polite_confusion / ask_check_question @#3: "Ask them to restate"   <- wrong_type:polite_confusion
OK gen_genuine_disagreement_00  genuine_disagreement / acknowledge_objection @#3: "They disagree. Acknowledge."
OK gen_genuine_disagreement_01  genuine_disagreement / acknowledge_objection @#1: "They disagree. Acknowledge."
OK gen_indirect_refusal_00      indirect_refusal / label_emotion @#1: "Name the hesitation"
OK gen_indirect_refusal_01      indirect_refusal / label_emotion @#1: "Name the hesitation"
OK gen_neutral_00               silent
OK gen_neutral_01               silent
OK gen_polite_confusion_00      polite_confusion / ask_check_question @#3: "Ask them to restate"
OK gen_polite_confusion_01      polite_confusion / ask_check_question @#1: "Ask them to restate"
OK gen_term_ambiguity_00        term_ambiguity / clarify_definition @#1: "Define the term"
OK gen_term_ambiguity_01        term_ambiguity / clarify_definition @#1: "Define the term"

{"interpreter": "fallback", "n": 12, "false_intervention_rate": 0.0, "seeded_detection_rate": 1.0,
 "type_accuracy": 0.8, "scenarios_with_failures": 2, "interpret_calls": 10,
 "interpret_p50_s": 0.0, "interpret_p95_s": 0.0}
```

`runs/eval_muse_generated.json` (reasoning_effort=minimal):

```
XX gen_escalating_friction_00   term_ambiguity / clarify_definition @#1: "Clarify three includes Jake"   <- wrong_turn:1 not in [3, 5], wrong_type:term_ambiguity
OK gen_escalating_friction_01   unanswered_objection / acknowledge_objection @#3: "Pause, address cloud ban"
OK gen_genuine_disagreement_00  genuine_disagreement / acknowledge_objection @#3: "Accept their no gracefully"
OK gen_genuine_disagreement_01  genuine_disagreement / acknowledge_objection @#1: "Name their concern"
OK gen_indirect_refusal_00      indirect_refusal / invite_no @#1: "Invite a no"
OK gen_indirect_refusal_01      indirect_refusal / invite_no @#1: "Invite a no"
OK gen_neutral_00               silent
OK gen_neutral_01               silent
OK gen_polite_confusion_00      term_ambiguity / ask_check_question @#3: "Check seat meaning"
OK gen_polite_confusion_01      polite_confusion / ask_check_question @#1: "Check shared meaning"
OK gen_term_ambiguity_00        term_ambiguity / clarify_definition @#1: "Clarify cash runway"
OK gen_term_ambiguity_01        term_ambiguity / clarify_definition @#1: "Define header simply"

{"interpreter": "muse", "n": 12, "false_intervention_rate": 0.0, "seeded_detection_rate": 1.0,
 "type_accuracy": 0.9, "scenarios_with_failures": 1, "interpret_calls": 13,
 "interpret_p50_s": 4.433, "interpret_p95_s": 6.944,
 "muse_usage": {"calls": 22, "input_tokens": 13419, "output_tokens": 10467, "cost_usd": 0.06126}}
```

### Reading the numbers

* **Neither interpreter ever intervened on a neutral conversation** (5 neutral scenarios across both sets,
  5 Muse runs). That is the property the product most depends on.
* On the hand-authored set the rules + templates are already perfect, so Muse can only lose there; its one
  miss (`polite_confusion_02`) is the model deciding "insufficient evidence", which the architecture treats
  as a legitimate answer. On the held-out generated set Muse is more accurate on type (0.9 vs 0.8) and its
  cues are specific to the conversation ("Define escrow simply", "Check seat meaning") where the templates
  are generic.
* The `gen_escalating_friction_00` fixture is nominated by the rules on turn 1 as term ambiguity
  ("three ways" is genuinely queried) before the friction pattern exists, so both interpreters "fail" it
  the same way; the Muse-authored `expect` block puts the acceptable turns at 3 and 5.
* **Latency is the open problem.** With `minimal` reasoning the interpreter p50 is 4.4–5.5 s and p95 up to
  6.9 s against a 2 s target and the Coach's 8 s staleness cut-off (the eval disables that cut-off with
  `stale_after_s=60`). With `low` reasoning p50 was 13.7 s, which would have every cue dropped as stale.
  Output tokens are ~90% reasoning even at `minimal`. The next lever is `muse-spark-1.3-contributor`
  (not used, Standard mode is a product requirement) or trimming the interpreter schema/prompt.

## Cost

| Item | calls | USD |
|---|---|---|
| Chat probes while diagnosing the empty-content bug (incl. the failing first smoke run) | 10 | ≈ 0.008 |
| `smoke_muse.py` chat (15 in / 265 out tokens, 3.6 s) | 1 | 0.00114 |
| `eval --muse`, hand-authored: low run 1 / low run 2 (crashed mid-run, meter not printed) / minimal | 13 / ≤13 / 13 | 0.0806 / ≤0.08 / 0.0336 |
| `gen_scenarios.py --per-type 2` | 12 | 0.0435 |
| `eval --muse`, generated: low run 1 / low run 2 / minimal | 20 / 21 / 22 | 0.1275 / 0.1334 / 0.0613 |
| Voice Transcribe: 7 sessions × 23 s at $0.18/h | 7 | 0.008 |
| **Total** | | **≈ 0.50–0.58** |

Per-call cost of one structured interpreter call at `minimal`: ≈ $0.0026 (≈ 600 in / 430 out tokens).

## Protocol corrections made (all isolated to the Muse client or its scripts)

1. **Reasoning tokens share `max_completion_tokens`** (`chat.py`). Muse Spark reasons before every answer;
   when the budget runs out during reasoning the API returns 200 with `finish_reason: "length"` and
   `content: null`, which the client previously surfaced as "no JSON object found in model output: ''". Now
   `max_tokens` is the visible-output budget, `REASONING_HEADROOM_TOKENS` (1536) is added on top,
   `reasoning_effort` is sent (default `minimal`, env `LLAMA_REASONING_EFFORT`), an exhausted budget raises a
   `MuseChatError` with `budget_exhausted=True`, and `complete()` retries that case once with `minimal`
   effort and double the budget. Both attempts are metered.
2. **End-of-stream marker** (`transcribe.py`). After `endStream` the server sends
   `{"type":"transcript","final":true,"transcript":""}` in every mode, not only PUSH_TO_TALK. The normaliser
   used to emit a spurious empty `final`/`turn_end` for it; it now ignores an empty final when no turn is open.
3. **Timing semantics** (`transcribe.py` comments, `MUSE_API.md`). `speechStart`/`speechEnd.audioProcessedMs`
   are the true onset/offset, so `t_start`/`t_end` are exact; the previous "approximate, late by one chunk"
   caveat was wrong.
4. **Confirmed as assumed**: `json_schema.name` is required; speaker labels are `A`/`B`; `speaker` precedes
   `speechEnd`/`speechComplete`; list-models is the OpenAI shape. Docs updated from UNVERIFIED to VERIFIED.
5. **Scripts**: `eval_scenarios.py` gained `--dir` (held-out sets), a `muse_usage` block in the summary, and
   wall-clock interpreter latency (the previous p50/p95 were always null because the Coach timed calls with
   the scripted replay clock). `smoke_muse.py` additionally reports speech-offset → final latency, since
   last-byte → final goes negative when a clip ends in silence.
6. **Tests**: `tests/test_muse_mock.py` updated for the new request shape; added tests for the budget-exhausted
   retry and the end-of-stream marker (39 pass).


---

# Update 2026-11-04: two-tier coaching (fixed cues + evidence-gated, streamed, speculative Muse)

The 5-7 s interpreter call measured above sat between the counterpart's last word and the wearer's next
one. This update takes it off that path. Code: `engine/evidence.py` (new), `engine/coach.py`,
`engine/interpreter.py`, `engine/pipeline.py`, `muse/chat.py` (`stream`), `scripts/eval_scenarios.py`
(`--partials`, `--realtime`, `--tier1-always`). 55 unit tests pass (12 new in `tests/test_engine_tiers.py`).

## What changed

| Piece | Before | Now |
|---|---|---|
| Every nomination | one Muse call, cue after it returns (p50 5.5 s) | **Tier 0**: fixed-vocabulary cue (`CUE_VOCABULARY`, six 2-4 word cues from the templates) emitted immediately, no model |
| When Muse runs | always | **Tier 1**: only when `EvidenceAccumulator` fires: accumulated score >= 1.0 (nominations add their rule score, +0.2 for a repeated type, x0.5 decay per quiet turn, +0.6 when a cue is judged unresolved) or a single nomination >= 0.9 (e.g. a question asked for the third time) |
| How Muse runs | awaited inline | background task; cue delivered at the next listening window (nobody speaking), dropped if > 8 s old or two counterpart turns later; outcome judging waits at most 50 ms inline then continues in the background |
| Request | 690-token prompt, 11 fields, non-streamed | 6-turn context, 7 short fields, streamed; the earpiece fires on the first three fields |
| Field order | assumed schema order | **Muse emits keys alphabetically** (verified live), so the early fields are named `act`, `cue`, `how` and sort ahead of `say`, `sure`, `test`, `type` |
| Start time | at the final transcript | **speculative**: on a counterpart partial, when the rules already nominate and the accumulator would escalate; kept only if the final transcript has the same nomination and compatible text (prefix or >= 80 % token overlap) |

What streaming buys is small and precisely measured: reasoning is not streamed, so nothing arrives
between the headers (~2 s) and the first visible token; the early fields then land 0.2-0.4 s before the
full object. The real wins are not waiting for the model at all (Tier 0) and starting it earlier
(speculation).

## Quality, unpaced replay (Tier 1 allowed to finish before the next turn)

| set | metric | rules-only | Muse (evidence-gated) |
|---|---|---|---|
| hand-authored (14) | false intervention / detection / type acc | 0.0 / 1.0 / 1.0 | 0.0 / 1.0 / 1.0 |
| hand-authored | Tier 0 cues / Tier 1 calls / Tier 1 cues | 10 / 1 / 1 | 10 / 1 / 1 |
| hand-authored | Muse cost | – | $0.0072 |
| generated (12) | false intervention / detection / type acc | 0.0 / 1.0 / 0.8 | 0.0 / 1.0 / 0.8 |
| generated | Tier 0 cues / Tier 1 calls / Tier 1 cues | 13 / 2 / 2 | 13 / 3 / 3 |
| generated | Muse cost | – | $0.0302 |

Type accuracy is scored at the first intervention moment, which is now the deterministic Tier 0 cue in
every scenario, so rules and Muse tie by construction; the two generated escalating-friction fixtures
fail for both as before (the rules nominate term ambiguity on turn 1, before the friction pattern exists).
Tier 1 fired on the sustained cases only: `indirect_refusal_01` (two refusals in a row), and on the
generated set the second nominations in `gen_escalating_friction_00` and `gen_indirect_refusal_00`.
Muse's Tier 1 cues there: "Invite a no" / "Would it be a problem to decline Friday?", "Ask what's unclear".

## Timing, paced replay (`--realtime --speed 1.5 --tier1-always`, hand-authored set, 8 s stale cut-off)

`--tier1-always` forces a Muse call on every nomination so there are nine calls to time; `ready` is the
wall time from the trigger turn's end to the Tier 1 cue being delivered (Tier 0 is always ~0 here).

| run | Tier 1 calls / cues / silent / dropped | call p50 / p95 | early fields p50 | **ready after turn p50 / p95** | speculative started / kept / discarded |
|---|---|---|---|---|---|
| no partials | 9 / 7 / 2 / 0 | 3.69 / 6.18 s | 3.46 s | **3.42 / 5.90 s** | 0 / 0 / 0 |
| `--partials 2` | 9 / 6 / 2 / 1 | 4.31 / 7.00 s | 4.20 s | **3.20 / 4.61 s** | 5 / 5 / 0 |

Per scenario, ready-after-turn (s), no partials -> with partials: jargon_01 2.74 -> 1.94, indirect_refusal_02
3.64 -> 2.39, polite_confusion_01 5.38 -> 4.61, escalation_01 2.95 -> 3.81 (the call itself was 1.4 s
slower), genuine_disagreement_02 5.90 -> silent. Where speculation fired it saved 0.8-1.3 s at 1.5x
playback, i.e. roughly 1.2-1.9 s at real speed, and every speculative call was confirmed by the final
transcript. It did not fire when the nominating signal only appears at the end of the turn (the question
mark, the hedging clause); with two coarse partials per turn in the harness that was 4 of 9 cases. Live
partials arrive every 160-400 ms, so coverage should be better than the harness shows.

Both runs cost ~$0.030. Across all runs today Muse spend was ≈ $0.14 (session total ≈ $0.69).

## Where the time goes now, and what is left

| moment | time after the counterpart stops speaking |
|---|---|
| Voice Transcribe final text | ~0.6 s |
| **Tier 0 cue in the earpiece** | **~0.7 s** |
| Tier 1 cue, when it runs | 3.2 s p50, 4.6 s p95 (with speculation), delivered at the next pause |

The remaining term is the model itself: 3.1-7.0 s per call at `reasoning_effort=minimal`, with the
variance coming from hidden reasoning tokens (210-500 on this prompt). Nothing in the API shortens that
further for Spark. The product consequence is the one the design assumes: Tier 0 is the always-on
signal, Tier 1 is for the occasional sustained moment, and its sentence is meant for the next pause, not
the next breath.
