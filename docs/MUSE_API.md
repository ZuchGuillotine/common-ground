# Meta Model API (Muse) — wire protocol notes

Written 2026-11-03 from the public docs; **not yet verified against a live key** (no key was available in
the authoring session). Anything marked UNVERIFIED is a documented-but-untested assumption and is isolated
to a single constant/function in code so it can be fixed in one place.

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
* Models list: `GET {base}/models` (https://dev.meta.ai/docs/api-reference/models/list-models); the response
  schema is not shown on that page, we assume the OpenAI `{"data":[{"id":...}]}` shape (UNVERIFIED, handled
  leniently in `MuseChat.list_models`).

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

### Audio

* Raw signed 16-bit little-endian mono PCM in **binary** frames; no container, timestamps or end marker.
  `PCM_16KHZ` = 32,000 B/s, `PCM_24KHZ` = 48,000 B/s. Cookbook uses 80 ms frames (2,560 B at 16 kHz).
* Must be sent at ~real time: the server closes with 1008 if the client is >5 s ahead of processing or if
  ingress stays below real time for 10 s. `MuseTranscribe(pace=True)` throttles sends for file replay.
* End of input: text frame `{"type": "endStream"}`; send nothing after it; keep reading until the server
  closes with 1000 (closing early can drop pending events).

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

Close codes: 1000 normal; 1008 invalid request / pacing violation (do not retry unchanged); 1011 backend
failure or "Max session duration reached" (retry with backoff / new session); 1013 rate limited (back off).
Limits: 60 min max session; idle input or stalled output closes the session.

### Normalisation performed by `TranscriptNormaliser` (transcribe.py)

* `partial` ← `transcript{final:false}`; `t_start` = `speechStart.audioProcessedMs/1000` of the current turn,
  `t_end` = this event's `audioProcessedMs/1000`.
* `final` + `turn_end` ← `speechComplete` (ENDPOINTING/DIARIZATION) or `transcript{final:true}` (PUSH_TO_TALK);
  `t_end` = `speechEnd.audioProcessedMs/1000` when seen, else the completing event's.
* `speaker_change` ← `speaker` when the label differs from the previous one; the label is attached to the
  current open turn and inherited by following turns until the next `speaker` event.
* Speaker labels are mapped to `S0`, `S1`, … in first-seen order (`normalise_speaker_label`). UNVERIFIED: the
  docs only say labels are bare letters like `A`.

### Known unknowns (need a key to confirm)

1. Whether `speechStart.audioProcessedMs` is the speech onset or merely the amount of audio processed when the
   event was emitted (the batch endpoint has true `startMs`/`endMs`; the stream has only `audioProcessedMs`).
   Our `t_start`/`t_end` are therefore approximate, probably late by up to one processing chunk.
2. Whether a `speaker` event can arrive after `speechEnd`/`speechComplete` for its turn. We attach it to the
   turn that is open at that moment (per the cookbook's documented order: speechStart → partials → speaker →
   speechEnd → speechComplete).
3. Exact `ListModelsResponse` shape.
4. Whether `response_format.json_schema.name` is required (we always send it).
