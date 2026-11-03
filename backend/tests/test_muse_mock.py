import json
import time

import httpx
import pytest

from common_ground.muse import (
    ChatClient,
    MockMuseChat,
    MockMuseTranscribe,
    MuseChat,
    MuseChatError,
    TranscribeClient,
    TranscriptNormaliser,
    UsageMeter,
    extract_json,
)
from common_ground.muse.models import MUSE_SPARK_1_3, price_for
from common_ground.muse.transcribe import MuseTranscribeError

TURNS = [
    {"speaker": "S0", "text": "We are offering an integration not a replacement", "t_start": 0.0, "t_end": 2.5},
    {"speaker": "S1", "text": "Wait what do you mean by integration", "t_start": 3.0, "t_end": 4.5},
    {"speaker": "S1", "text": "Sure I guess", "t_start": 5.0, "t_end": 5.6},
]


# ------------------------------------------------------------------ extractor
@pytest.mark.parametrize("text", [
    '{"a": 1, "b": "x}y"}',
    'Here you go:\n```json\n{"a": 1, "b": "x}y"}\n```\nDone.',
    'prefix {"a": 1, "b": "x}y"} suffix {"c": 2}',
    'escaped: {"a": 1, "b": "x}y", "q": "say \\"hi\\" {"} ',
])
def test_extract_json_variants(text):
    assert extract_json(text)["a"] == 1


def test_extract_json_failure():
    with pytest.raises(MuseChatError):
        extract_json("no json here, just [1, 2]")


# ---------------------------------------------------------------- usage meter
def test_usage_meter_cost():
    m = UsageMeter()
    m.add(1_000_000, 1_000_000, price_for(MUSE_SPARK_1_3))
    assert m.cost_usd == pytest.approx(1.25 + 4.25)
    assert m.calls == 1 and "1000000 in" in m.summary()


# ------------------------------------------------------------------ mock chat
async def test_mock_chat_dict_and_callable():
    chat = MockMuseChat({"integration": {"type": "term_ambiguity"}}, default={"type": "none"})
    assert isinstance(chat, ChatClient)
    out = await chat.complete_json([{"role": "user", "content": "what do you mean by integration"}])
    assert out == {"type": "term_ambiguity"}
    assert await chat.complete_json([{"role": "user", "content": "hello"}]) == {"type": "none"}
    assert chat.usage.calls == 2 and chat.usage.cost_usd > 0
    assert len(chat.calls) == 2

    fn = MockMuseChat(lambda msgs, schema: {"n": len(msgs), "schema": (schema or {}).get("title")})
    out = await fn.complete_json([{"role": "user", "content": "x"}], json_schema={"title": "T"})
    assert out == {"n": 1, "schema": "T"}
    assert json.loads(await fn.complete([{"role": "user", "content": "x"}]))["n"] == 1


# ------------------------------------------------------------ mock transcribe
async def test_mock_transcribe_event_sequence():
    async with MockMuseTranscribe(TURNS) as t:
        assert isinstance(t, TranscribeClient)
        await t.send_pcm(b"\0" * 3200)
        await t.end_stream()
        events = [e async for e in t.events()]
    kinds = [e.kind for e in events]
    assert kinds.count("final") == 3 and kinds.count("turn_end") == 3 and kinds.count("speaker_change") == 2
    assert kinds.index("speaker_change") < kinds.index("final")
    finals = [e for e in events if e.kind == "final"]
    assert [f.text for f in finals] == [u["text"] for u in TURNS]
    assert [f.speaker_label for f in finals] == ["S0", "S1", "S1"]
    assert finals[0].t_start == 0.0 and finals[0].t_end == 2.5 and finals[0].turn_id == 0
    partials = [e for e in events if e.kind == "partial" and e.turn_id == 0]
    assert partials and all(TURNS[0]["text"].startswith(p.text) for p in partials)
    assert len(partials[-1].text) > len(partials[0].text)   # partials are revised/cumulative
    for p in partials:
        assert p.t_end is not None and p.t_start is not None and p.t_start <= p.t_end
    # final follows turn_end immediately, same turn
    for i, e in enumerate(events):
        if e.kind == "final":
            assert events[i + 1].kind == "turn_end" and events[i + 1].turn_id == e.turn_id
    assert t.bytes_sent == 3200


async def test_mock_transcribe_pacing():
    turns = [{"speaker": "S0", "text": "a b", "t_start": 0.0, "t_end": 0.4}]
    t0 = time.monotonic()
    async with MockMuseTranscribe(turns, speed=4.0) as t:
        events = [e async for e in t.events()]
    elapsed = time.monotonic() - t0
    assert 0.08 <= elapsed < 0.5 and events[-1].kind == "turn_end"


# ----------------------------------------------- real-client normaliser (offline)
def test_normaliser_diarization_sequence():
    n = TranscriptNormaliser()
    evs = []
    wire = [
        {"type": "speechStart", "turnId": 1, "audioProcessedMs": 500},
        {"type": "transcript", "transcript": "we are", "final": False, "audioProcessedMs": 900},
        {"type": "transcript", "transcript": "we are offering", "final": False, "audioProcessedMs": 1400},
        {"type": "speaker", "label": "A", "audioProcessedMs": 1400},
        {"type": "speechEnd", "turnId": 1, "audioProcessedMs": 2500},
        {"type": "speechStart", "turnId": 2, "audioProcessedMs": 3000},
        {"type": "audioProgress", "audioProcessedMs": 3100},
        {"type": "transcript", "transcript": "wait", "final": False, "audioProcessedMs": 3300},
        {"type": "speechComplete", "turnId": 1, "transcript": "We are offering.", "audioProcessedMs": 3300},
        {"type": "speaker", "label": "B", "audioProcessedMs": 3400},
        {"type": "speaker", "label": "B", "audioProcessedMs": 3500},
        {"type": "speechEnd", "turnId": 2, "audioProcessedMs": 4500},
        {"type": "speechComplete", "turnId": 2, "transcript": "Wait.", "audioProcessedMs": 4600},
        {"type": "unknownFutureEvent", "x": 1},
    ]
    for raw in wire:
        evs += n.feed(raw)
    kinds = [e.kind for e in evs]
    assert kinds == ["partial", "partial", "speaker_change", "partial", "final", "turn_end",
                     "speaker_change", "final", "turn_end"]
    assert evs[1].text == "we are offering" and evs[1].t_start == 0.5 and evs[1].t_end == 1.4
    f1 = evs[4]
    assert (f1.text, f1.speaker_label, f1.t_start, f1.t_end, f1.turn_id) == ("We are offering.", "S0", 0.5, 2.5, 1)
    f2 = evs[7]
    assert (f2.text, f2.speaker_label, f2.t_start, f2.t_end, f2.turn_id) == ("Wait.", "S1", 3.0, 4.5, 2)
    assert evs[6].speaker_label == "S1" and evs[6].raw["label"] == "B"


def test_normaliser_push_to_talk_and_error():
    n = TranscriptNormaliser()
    assert n.feed({"type": "transcript", "transcript": "hi", "final": False, "audioProcessedMs": 100})[0].kind == "partial"
    evs = n.feed({"type": "transcript", "transcript": "Hi.", "final": True, "audioProcessedMs": 800})
    assert [e.kind for e in evs] == ["final", "turn_end"] and evs[0].text == "Hi." and evs[0].t_end == 0.8
    with pytest.raises(MuseTranscribeError):
        n.feed({"type": "error", "message": "bad", "sessionId": "s"})


# ------------------------------------------------ real chat client via MockTransport
async def test_muse_chat_request_shape_and_retry(monkeypatch):
    monkeypatch.setattr("common_ground.muse.chat._backoff", lambda *_: 0.0)
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if len(seen) == 1:
            return httpx.Response(429, json={"error": {"message": "slow down", "type": "rate_limit_error"}},
                                  headers={"Retry-After": "0"})
        return httpx.Response(200, json={
            "choices": [{"index": 0, "message": {"role": "assistant", "content": '{"answer": "Paris"}'},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    chat = MuseChat(api_key="LLM_1_test", base_url="https://api.meta.ai/v1", client=client)
    schema = {"title": "Answer", "type": "object", "properties": {"answer": {"type": "string"}}}
    out = await chat.complete_json([{"role": "user", "content": "Capital of France?"}], json_schema=schema,
                                   max_tokens=32)
    assert out == {"answer": "Paris"}
    assert len(seen) == 2
    req = seen[-1]
    assert str(req.url) == "https://api.meta.ai/v1/chat/completions"
    assert req.headers["Authorization"] == "Bearer LLM_1_test"
    body = json.loads(req.content)
    # verified live: reasoning tokens share max_completion_tokens, so the client adds headroom + reasoning_effort
    from common_ground.muse.chat import DEFAULT_REASONING_EFFORT, REASONING_HEADROOM_TOKENS
    assert body["model"] == MUSE_SPARK_1_3 and "temperature" not in body
    assert body["max_completion_tokens"] == 32 + REASONING_HEADROOM_TOKENS
    assert body["reasoning_effort"] == DEFAULT_REASONING_EFFORT
    assert body["response_format"] == {"type": "json_schema", "json_schema": {"name": "Answer", "schema": schema}}
    assert (chat.usage.input_tokens, chat.usage.output_tokens, chat.usage.calls) == (10, 5, 1)
    assert chat.usage.cost_usd == pytest.approx((10 * 1.25 + 5 * 4.25) / 1e6)


async def test_muse_chat_no_retry_on_400():
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": {"message": "bad schema", "type": "invalid_request_error"}})

    chat = MuseChat(api_key="k", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(MuseChatError, match="bad schema"):
        await chat.complete([{"role": "user", "content": "x"}])
    assert calls == 1


def test_muse_chat_requires_key(monkeypatch):
    monkeypatch.delenv("LLAMA_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    with pytest.raises(MuseChatError, match="LLAMA_API_KEY"):
        MuseChat()


async def test_muse_chat_budget_exhausted_by_reasoning_retries_minimal_then_errors():
    # verified live: finish_reason="length" with content=null when reasoning eats the whole budget
    from common_ground.muse.chat import REASONING_HEADROOM_TOKENS
    bodies: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={
            "choices": [{"index": 0, "message": {"role": "assistant", "content": None}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 15, "completion_tokens": 64, "total_tokens": 79,
                      "completion_tokens_details": {"reasoning_tokens": 62}}})

    chat = MuseChat(api_key="k", reasoning_effort="low",
                    client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(MuseChatError, match="budget exhausted"):
        await chat.complete([{"role": "user", "content": "hi"}], max_tokens=64)
    assert [b["reasoning_effort"] for b in bodies] == ["low", "minimal"]
    assert bodies[0]["max_completion_tokens"] == 64 + REASONING_HEADROOM_TOKENS
    assert bodies[1]["max_completion_tokens"] == 2 * (64 + REASONING_HEADROOM_TOKENS)
    assert chat.usage.calls == 2   # both attempts are billed


def test_normaliser_ignores_end_of_stream_final_marker():
    # verified live: after endStream the server sends an empty transcript{final:true} in every mode
    from common_ground.muse.transcribe import TranscriptNormaliser
    n = TranscriptNormaliser()
    n.feed({"type": "speechStart", "audioProcessedMs": 60, "turnId": 0})
    n.feed({"type": "transcript", "transcript": " hi", "final": False, "audioProcessedMs": 800})
    n.feed({"type": "speaker", "label": "A", "audioProcessedMs": 1200})
    n.feed({"type": "speechEnd", "audioProcessedMs": 700, "turnId": 0})
    evs = n.feed({"type": "speechComplete", "audioProcessedMs": 700, "turnId": 0, "transcript": " hi "})
    assert [e.kind for e in evs] == ["final", "turn_end"] and evs[0].speaker_label == "S0"
    assert n.feed({"type": "transcript", "transcript": "", "final": True, "audioProcessedMs": 1300}) == []
