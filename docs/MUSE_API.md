# Meta Model API (Muse) — wire protocol notes

Written 2026-11-03 from the public docs and **verified against a live key the same day** (chat, structured
output, list-models, and the Voice Transcribe realtime WebSocket in DIARIZATION and ENDPOINTING modes, using a
23.7 s two-voice synthetic clip, `fixtures/audio/synthetic_two_speakers.wav`). Items marked VERIFIED were
observed on the wire; the measured numbers and the eval results are in [RESULTS.md](RESULTS.md).

Implementation: `backend/common_ground/muse/` (`chat.py`, `transcribe.py`, `models.py`).

## Hosts, auth, env vars

| Item | Value | Source |
|---|---|---|
| REST base URL | `https://api.meta.ai/v1` | https://dev.meta.ai/docs/quickstart |
| Auth (REST) | `Authorization: Bearer <key>`; keys look like `LLM_<id>_<secret>` | https://dev.meta.ai/docs/authentication |
| Env var in docs | `MODEL_API_KEY` | https://dev.meta.ai/docs/authentication |
| Env vars we read | `LLAMA_API_KEY` (falls back to `MODEL_API_KEY`); `LLAMA_API_BASE_URL` (falls back to `MODEL_API_BASE_URL`, then the default above); `LLAMA_ASR_REALTIME_URL` | project convention |
| Old host | `api.llama.com` (Llama API) was retired; Muse is served from `api.meta.ai` | https://docs.litellm.ai/docs/providers/meta (secondary) |

## Models and pricing

Source: https://dev.meta.ai/docs/models and https://dev.meta.ai/docs/pricing-rate-limits

| Model id | Notes | $/1M in | $/1M out | cached in |
|---|---|---|---|---|
| `muse-spark-1.3` | "Recommended for new work" — our default chat model | 1.25 | 4.25 | 0.15 |
| `muse-spark-1.3-contributor` | cheaper "contributor mode", 150 RPM | 0.10 | 0.20 | 0.002 |
| `muse-spark-1.2`, `muse-spark-1.1` | older | 1.25 | 4.25 | 0.15 |
| `muse-wonder-1.0` | preview, bigger | 4.00 | 20.00 | 0.20 |
| `muse-voice-transcribe-1.0` | STT only; realtime WS + `POST /v1/asr/transcribe` | $0.18 / hour of audio (whole seconds, rounded down) | | |

Rate limits (per team): Muse Spark 3,000 RPM / 4M TPM; Voice Transcribe 256 concurrent streams, 16,000 streams/hour.
Context window 1,048,576 tokens; reasoning tokens are billed as output tokens.

## Chat completions (used by `MuseChat`)

Source: https://dev.meta.ai/docs/protocols/chat-completions

* `POST {base}/chat/completions`, JSON body, OpenAI-compatible.
* Request fields we send: `model`, `messages` (`role` in `system|developer|user|assistant|tool`, `content`),
  `max_completion_tokens` (the `max_tokens` alias is deprecated), optional `temperature` (0–2, default 1.0;
  docs say Muse Spark is tuned for the default, so we only send it when the caller sets it), optional
  `response_format`.
* Not supported (HTTP 400): `stop`, `n>1`, `logit_bias`, `logprobs`, `frequency_penalty`, `presence_penalty`,
  `reasoning_effort: "none"`, Responses-style `text.format`, `max_output_tokens`.
* Response: `choices[0].message.content` (string), `choices[0].finish_reason`,
  `usage.prompt_tokens` / `usage.completion_tokens` / `usage.total_tokens`.
* Models list: `GET {base}/models` (https://dev.meta.ai/docs/api-reference/models/list-models). VERIFIED shape:
  `{"object": "list", "data": [{"id": "muse-spark-1.3", "object": "model", "created": 0, "owned_by": "meta"}, ...]}`.
  The live catalogue also lists `sam-3.1`, `muse-image-1.0` and `muse-spark-1.2-contributor`.
* VERIFIED `usage` shape: `prompt_tokens`, `completion_tokens`, `total_tokens`, plus
  `completion_tokens_details.reasoning_tokens` and `prompt_tokens_details.cached_tokens`.

### Reasoning tokens vs `max_completion_tokens` (VERIFIED, the one real surprise)

Muse Spark always reasons before answering, the reasoning tokens are billed as output **and count against
`max_completion_tokens`**. When the budget runs out during reasoning the response is HTTP 200 with
`finish_reason: "length"` and `message.content: null` (no partial text, no error). Measured on
"Capital of France? One word." with a JSON schema:

| `reasoning_effort` | reasoning tokens | visible result at `max_completion_tokens=64` |
|---|---|---|
| `minimal` | 56 | `content: null`, `finish_reason: length` |
| `low` | 201 | `content: null` |
| `medium` (server default looks like this) | 334 | `content: null` |
| any, at 400+ | | `{"answer":"Paris","confidence":1}` |

On the coaching interpreter prompt, `low` spent 1,000-2,500 reasoning tokens per call (p50 latency 13.7 s,
p95 21.6 s) and once exhausted a 2,560-token budget. `reasoning_effort: "none"` is rejected with HTTP 400, as
the docs say; `minimal|low|medium|high|xhigh` are accepted.

What `MuseChat` does about it (`chat.py`):
* `max_tokens` means the *visible* output budget; `REASONING_HEADROOM_TOKENS` (1536, env
  `LLAMA_REASONING_HEADROOM`) is added to `max_completion_tokens`.
* `reasoning_effort` defaults to `minimal` (env `LLAMA_REASONING_EFFORT`); this is a latency-bound loop.
* `content: null` + `finish_reason: length` raises `MuseChatError(budget_exhausted=True)`; `complete()` retries
  that case once with `minimal` effort and twice the budget, then raises. Both attempts are metered.

### Structured output

Source: https://dev.meta.ai/docs/structured-output

```json
"response_format": {"type": "json_schema",
                    "json_schema": {"name": "Address", "schema": { ...JSON Schema... }, "strict": false}}
```
Output is always constrained to the schema; `strict: true` only adds validation of the schema against a strict
subset (root must be an object, every object needs `additionalProperties: false`, every property must be in
`required`, no `allOf`/`oneOf`, no recursion, depth <= 10). We send `strict` unset (= false) so ordinary
schemas are accepted. `complete_json()` still runs the tolerant extractor on the returned text; with no schema
it prepends a "reply with a single JSON object" system message.

VERIFIED: `json_schema.name` **is required**; without it the API returns HTTP 400
`` `response_format` missing required field `name` ``. A `title` key inside the schema itself is accepted.
The returned `content` is compact JSON exactly matching the schema (e.g. `{"answer":"Paris","confidence":1}`).

VERIFIED: **keys are emitted in alphabetical order, not schema order.** A schema declared as
`intervene, strategy, earpiece_cue, suggestion, note` streamed back as `earpiece_cue, intervene, note,
strategy, suggestion`. To get fields early in a stream, name them so they sort first (the interpreter
uses `act`, `cue`, `how` ahead of `say`, `sure`, `test`, `type`). `description` keys in the schema are accepted.

### Streaming (VERIFIED, used by `MuseChat.stream`)

`"stream": true, "stream_options": {"include_usage": true}` returns `text/event-stream`. Each event is
`data: {"id":..,"choices":[{"delta":{"content":"..."},"finish_reason":null,"index":0}],...}`; the first
delta also carries `"role":"assistant"`; the last choice chunk has `"delta":{}`, `"finish_reason":"stop"`
and the `usage` object; then `data: [DONE]`. Reasoning is **not** streamed: nothing arrives between the
headers (~2 s) and the first visible token (~3.5-5 s), so time-to-first-token is not improved by streaming,
only the tail is. No `reasoning_content`/`reasoning` delta keys were ever seen.

### Errors and retries

Source: https://dev.meta.ai/docs/error-handling

Body: `{"error": {"message", "type", "param", "code"}}` with `type` in `invalid_request_error`,
`authentication_error`, `rate_limit_error`, `server_error`, `billing_error`. Retry 429/500/503 with exponential
backoff + jitter, honouring `Retry-After`; do not retry 400/401/402/403/404/413. 504 means the request was too
long (docs advise against blind retry). `MuseChat` retries 429 and all 5xx up to 3 times (0.5 s doubling +
jitter, `Retry-After` honoured, capped at 30 s).

## Voice Transcribe realtime WebSocket (used by `MuseTranscribe`)

Sources: https://dev.meta.ai/docs/speech-to-text , https://dev.meta.ai/docs/api-reference/voice/realtime ,
https://dev.meta.ai/docs/api-reference/voice/schemas , cookbook
https://github.com/meta-models/meta-model-cookbook/tree/main/06_muse_voice/01_voice_api_fundamentals

### Connection

* URL `wss://api.meta.ai/v1/asr/realtime` (optional `?sessionId=` query; server generates one otherwise).
* **The HTTP `Authorization` header is ignored.** The key goes in the handshake frame.
* Handshake = first JSON **text** frame, must arrive within 10 s of connecting; config is fixed for the session.

```json
{"authorization": {"accessToken": "Bearer LLM_..."},
 "audioEncoding": "PCM_16KHZ",          // or PCM_24KHZ (native). 16 kHz is resampled server-side.
 "model": "muse-voice-transcribe-1.0",
 "mode": "DIARIZATION",                 // PUSH_TO_TALK (default) | ENDPOINTING | DIARIZATION
 "partialMode": "CUMULATIVE",           // CUMULATIVE (default; each partial replaces the last) | DELTA
 "emitAudioProgress": false,            // default true: one audioProgress event per processed chunk
 "keywords": ["..."],                   // optional, biases recognition
 "languageBias": ["English"]}           // optional, English language names, not codes
```
* Handshake ack: `{"sessionId": "..."}` — the only server frame without a `type`. Wait for it before sending audio.
  VERIFIED: ids look like `voyager/duplex/AB1C644B7F0541D6A80223E7068FB6F8`; connect + ack took ~0.8 s.

### Audio

* Raw signed 16-bit little-endian mono PCM in **binary** frames; no container, timestamps or end marker.
  `PCM_16KHZ` = 32,000 B/s, `PCM_24KHZ` = 48,000 B/s. Cookbook uses 80 ms frames (2,560 B at 16 kHz).
* Must be sent at ~real time: the server closes with 1008 if the client is >5 s ahead of processing or if
  ingress stays below real time for 10 s. `MuseTranscribe(pace=True)` throttles sends for file replay.
* End of input: text frame `{"type": "endStream"}`; send nothing after it; keep reading until the server
  closes with 1000 (closing early can drop pending events). VERIFIED: after `endStream` the server flushes the
  pending `speaker`/`speechEnd`/`speechComplete`, then sends **`{"type": "transcript", "final": true,
  "transcript": "", "audioProcessedMs": N}` in every mode** (not only PUSH_TO_TALK) as an end-of-stream
  marker, then closes with 1000 and reason `No more transcript segments`. The normaliser ignores that marker.

### Server events (`type` discriminator; ignore unknown types)

| type | fields | meaning |
|---|---|---|
| `speechStart` | `turnId:int`, `audioProcessedMs:int` | a turn opened (ENDPOINTING/DIARIZATION) |
| `transcript` | `transcript:str`, `final:bool`, `audioProcessedMs` | partial text; CUMULATIVE replaces the previous partial. No `turnId` (belongs to the latest `speechStart`). `final:true` is the completion signal only in PUSH_TO_TALK |
| `speaker` | `label:str` (e.g. `"A"`), `audioProcessedMs` | DIARIZATION only; labels the span of speech that preceded it. Labels are session-local, not identities |
| `speechEnd` | `turnId`, `audioProcessedMs` | speech for the turn stopped |
| `speechComplete` | `turnId`, `transcript:str`, `audioProcessedMs` | finalized, post-processed turn text; may arrive late and out of order — match by `turnId` |
| `audioProgress` | `audioProcessedMs` | cumulative processed audio (useful for billing) |
| `error` | `message`, `sessionId`, optional `errorType`, `errorCode`, `errorParam` | sent just before the server closes |

Close codes: 1000 normal (VERIFIED, reason `No more transcript segments`); 1008 invalid request / pacing
violation (do not retry unchanged); 1011 backend failure or "Max session duration reached" (retry with
backoff / new session); 1013 rate limited (back off).
Limits: 60 min max session; idle input or stalled output closes the session.

### What the stream actually looked like (VERIFIED, DIARIZATION, two alternating voices)

Per turn, in this order every time: `speechStart{turnId}` → cumulative `transcript{final:false}` partials
(no `turnId`, ~every 160-400 ms of audio, text is lower-case and unpunctuated) → `speaker{label}` →
`speechEnd{turnId}` → `speechComplete{turnId, transcript}`, the last three in one burst. Then the next
`speechStart`. `audioProgress` interleaves when enabled.

* `speaker.label` is `"A"`, `"B"` (bare letters, session-local). On the clip both voices were labelled
  correctly on all 6 turns and the labels alternated with the voices.
* `speechStart.audioProcessedMs` is the **speech onset** (60 ms for a voice that starts at t=0), and
  `speechEnd.audioProcessedMs` is the **speech offset**: both were ~500 ms behind the processing position
  reported by the `speaker`/`transcript` events emitted in the same burst, i.e. the endpointer needs ~500 ms
  of trailing silence to close a turn. `t_start`/`t_end` in `TranscriptEvent` are therefore accurate, not
  approximate as first assumed.
* `speechComplete.transcript` is post-processed (capitalised, punctuated) in most sessions but was returned
  lower-case/unpunctuated in one of five sessions, so do not rely on punctuation.
* ENDPOINTING: no `speaker` events; otherwise identical. In one of three runs it merged two short turns
  spoken by different voices across a 0.7 s pause into one segment (DIARIZATION never did), so DIARIZATION is
  the better turn segmenter for a two-person conversation even if labels are ignored.
* Latency (16 kHz, paced real-time replay): the final `speechComplete` arrived 166-188 ms after the last
  audio byte was sent and ~500-610 ms after the speech offset; the ~500 ms is the endpointer's trailing
  silence, not network/compute. Interim partials trail the audio by well under a second.

### Normalisation performed by `TranscriptNormaliser` (transcribe.py)

* `partial` ← `transcript{final:false}`; `t_start` = `speechStart.audioProcessedMs/1000` of the current turn,
  `t_end` = this event's `audioProcessedMs/1000`.
* `final` + `turn_end` ← `speechComplete` (ENDPOINTING/DIARIZATION) or `transcript{final:true}` (PUSH_TO_TALK);
  `t_end` = `speechEnd.audioProcessedMs/1000` when seen, else the completing event's.
* `speaker_change` ← `speaker` when the label differs from the previous one; the label is attached to the
  current open turn and inherited by following turns until the next `speaker` event.
* Speaker labels are mapped to `S0`, `S1`, … in first-seen order (`normalise_speaker_label`). VERIFIED: the
  wire labels are `A`, `B`.
* The end-of-stream `transcript{final:true, transcript:""}` marker is dropped when no turn is open.

### Former known unknowns, now resolved live

1. `speechStart.audioProcessedMs` **is** the speech onset and `speechEnd.audioProcessedMs` the offset (see
   above); `t_start`/`t_end` are exact to the endpointer's resolution.
2. `speaker` arrives **before** `speechEnd`/`speechComplete` for its turn, in the same burst, so attaching it to
   the open turn is correct.
3. `ListModelsResponse` is the OpenAI shape `{"object":"list","data":[{"id",...}]}`.
4. `response_format.json_schema.name` **is required** (HTTP 400 without it).

New findings that required code changes: reasoning tokens count against `max_completion_tokens` (see the
chat section), and the end-of-stream `transcript{final:true}` marker is sent in every mode (see above).
