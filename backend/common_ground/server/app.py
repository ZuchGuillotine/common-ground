"""FastAPI + WebSocket server. One websocket = one coaching session.

Protocol (see schema.ClientMessage / ServerMessage):
  text frame  -> JSON ClientMessage  {type: start|text_turn|cue_played|assign_speaker|stop, ...}
  binary frame-> raw PCM16 mono 16 kHz audio (forwarded to Muse Voice Transcribe + ring buffer)
  server      -> JSON ServerMessage  {type: transcript|turn|state|cue|graph_event|error|metrics, payload, t}

Run:  uvicorn common_ground.server.app:app --host 0.0.0.0 --port 8000
Env:  LLAMA_API_KEY (else offline fallback interpreter), COMMON_GROUND_RUNS_DIR (default ./runs),
      COMMON_GROUND_TEXT_EMOTION=0 to skip the RoBERTa model, COMMON_GROUND_SPEECH_EMOTION=1 to enable wav2vec2.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from ..schema import ClientMessage, SessionConfig
from ..engine import Coach, ConversationGraph
from ..engine.fallback import FallbackInterpreter
from ..engine.interpreter import Interpreter
from ..engine.pipeline import Pipeline

app = FastAPI(title="Common Ground", version="0.1.0")
RUNS = Path(os.environ.get("COMMON_GROUND_RUNS_DIR", "runs"))
_models: dict = {}


def _signals():
    """Lazy, process-wide signal models."""
    if "linguistic" not in _models:
        from ..signals import LinguisticDetector
        _models["linguistic"] = LinguisticDetector()
        _models["text_emotion"] = None
        _models["acoustic"] = None
        _models["speech_emotion"] = None
        if os.environ.get("COMMON_GROUND_TEXT_EMOTION", "1") != "0":
            try:
                from ..signals import TextEmotionClassifier
                m = TextEmotionClassifier(); m.warmup(); _models["text_emotion"] = m
            except Exception as e:  # model download failure must not kill the server
                print(f"[signals] text emotion disabled: {e}")
        try:
            from ..signals import AcousticExtractor
            _models["acoustic"] = AcousticExtractor()
        except Exception as e:
            print(f"[signals] acoustic disabled: {e}")
        if os.environ.get("COMMON_GROUND_SPEECH_EMOTION", "0") == "1":
            try:
                from ..signals import SpeechEmotionClassifier
                _models["speech_emotion"] = SpeechEmotionClassifier()
            except Exception as e:
                print(f"[signals] speech emotion disabled: {e}")
    return _models


def _interpreter():
    if os.environ.get("LLAMA_API_KEY"):
        from ..muse import MuseChat
        return Interpreter(MuseChat())
    print("[muse] LLAMA_API_KEY not set: using offline FallbackInterpreter")
    return FallbackInterpreter()


def _transcriber():
    if not os.environ.get("LLAMA_API_KEY"):
        return None
    from ..muse import MuseTranscribe
    return MuseTranscribe(mode="DIARIZATION")


@app.get("/health")
async def health():
    return {"ok": True, "muse": bool(os.environ.get("LLAMA_API_KEY")), "version": app.version}


@app.get("/sessions/{session_id}")
async def session_graph(session_id: str):
    p = RUNS / f"{session_id}.jsonl"
    if not p.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


@app.websocket("/ws/session")
async def ws_session(ws: WebSocket):
    await ws.accept()
    t_zero = time.monotonic()

    async def emit(kind: str, payload: dict):
        await ws.send_text(json.dumps({"type": kind, "payload": payload, "t": round(time.monotonic() - t_zero, 3)}))

    cfg = SessionConfig(session_id=uuid.uuid4().hex[:8])
    coach: Coach | None = None
    pipe: Pipeline | None = None
    transcriber = None
    reader_task: asyncio.Task | None = None

    async def start(c: SessionConfig):
        nonlocal coach, pipe, transcriber, reader_task
        sig = _signals()
        coach = Coach(c, ConversationGraph(c.session_id, RUNS), _interpreter(), emit=emit)
        pipe = Pipeline(coach, sig["linguistic"], sig["text_emotion"], sig["acoustic"], sig["speech_emotion"])
        transcriber = _transcriber()
        if transcriber is not None:
            await transcriber.__aenter__()

            async def reader():
                async for ev in transcriber.events():
                    await pipe.on_transcript_event(ev)
            reader_task = asyncio.create_task(reader())
        await emit("state", {"state": coach.state.value, "session_id": c.session_id, "transcriber": transcriber is not None})

    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                if pipe is None:
                    continue
                pipe.feed_pcm(msg["bytes"])
                if transcriber is not None:
                    await transcriber.send_pcm(msg["bytes"])
                continue
            try:
                cm = ClientMessage.model_validate_json(msg.get("text") or "{}")
            except Exception as e:
                await emit("error", {"message": f"bad message: {e}"}); continue

            if cm.type == "start":
                cfg = cm.config or cfg
                if not cfg.consent_confirmed:
                    await emit("error", {"message": "consent_confirmed must be true to start"}); continue
                await start(cfg)
            elif pipe is None or coach is None:
                await emit("error", {"message": "send start first"})
            elif cm.type == "text_turn" and cm.text_turn:
                tt = cm.text_turn
                await pipe.on_text_turn(tt.get("speaker", "unknown"), tt["text"],
                                        float(tt.get("t_start", time.monotonic() - t_zero)),
                                        float(tt.get("t_end", time.monotonic() - t_zero)))
            elif cm.type == "cue_played" and cm.cue_window:
                coach.on_cue_played(*cm.cue_window)
            elif cm.type == "assign_speaker" and cm.speaker_map:
                pipe.assign_speakers(cm.speaker_map)
            elif cm.type == "stop":
                await emit("metrics", {**coach.metrics.summary(), "pipeline_latency": pipe.latency_log[-20:]})
                break
    except WebSocketDisconnect:
        pass
    finally:
        if reader_task:
            reader_task.cancel()
        if transcriber is not None:
            try:
                await transcriber.__aexit__(None, None, None)
            except Exception:
                pass
