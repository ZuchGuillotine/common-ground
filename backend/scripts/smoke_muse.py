#!/usr/bin/env python
"""Smoke test for the Muse clients. Needs LLAMA_API_KEY (or MODEL_API_KEY).

  python scripts/smoke_muse.py                       # list models + one tiny chat completion
  python scripts/smoke_muse.py --wav clip.wav        # also stream a 16 kHz mono WAV to Voice Transcribe
  python scripts/smoke_muse.py --wav clip.wav --mode ENDPOINTING
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import wave

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common_ground.muse import DEFAULT_CHAT_MODEL, MuseChat, MuseError, MuseTranscribe
from common_ground.muse.chat import api_key_from_env, base_url_from_env
from common_ground.muse.transcribe import PCM_16KHZ_BYTES_PER_SEC

CHUNK_MS = 80


async def smoke_chat(model: str) -> None:
    async with MuseChat(model=model) as chat:
        try:
            print("models:", ", ".join(await chat.list_models()))
        except MuseError as e:
            print(f"list models failed (non-fatal): {e}")
        schema = {"title": "Answer", "type": "object",
                  "properties": {"answer": {"type": "string"}, "confidence": {"type": "number"}},
                  "required": ["answer", "confidence"], "additionalProperties": False}
        t0 = time.perf_counter()
        out = await chat.complete_json([{"role": "user", "content": "Capital of France? One word."}],
                                       json_schema=schema, max_tokens=64)
        print(f"chat ({model}): {out}  [{time.perf_counter() - t0:.2f}s]")
        print("usage:", chat.usage.summary())


def read_wav_16k_mono(path: str) -> bytes:
    with wave.open(path, "rb") as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, 16000):
            sys.exit(f"{path}: need 16 kHz mono 16-bit PCM, got {w.getnchannels()}ch "
                     f"{w.getsampwidth() * 8}bit {w.getframerate()}Hz")
        return w.readframes(w.getnframes())


async def smoke_transcribe(path: str, mode: str) -> None:
    pcm = read_wav_16k_mono(path)
    chunk = PCM_16KHZ_BYTES_PER_SEC * CHUNK_MS // 1000
    print(f"streaming {len(pcm) / PCM_16KHZ_BYTES_PER_SEC:.1f}s of audio in {mode} mode ...")
    t_wall0 = time.monotonic()
    async with MuseTranscribe(mode=mode, pace=True) as t:   # type: ignore[arg-type]
        print("session:", t.session_id)

        async def pump() -> None:
            for i in range(0, len(pcm), chunk):
                await t.send_pcm(pcm[i:i + chunk])
            await t.end_stream()

        sender = asyncio.create_task(pump())
        last_final_at: float | None = None
        async for ev in t.events():
            now = time.monotonic() - t_wall0
            if ev.kind == "final":
                last_final_at = time.monotonic()
            if ev.kind == "partial":
                print(f"\r[{now:7.2f}s] {ev.speaker_label or '--'} ~ {ev.text[:90]:<90}", end="", flush=True)
            else:
                span = f"{ev.t_start if ev.t_start is not None else '?'}-{ev.t_end if ev.t_end is not None else '?'}s"
                print(f"\n[{now:7.2f}s] {ev.kind:14} {ev.speaker_label or '--':3} {span:14} {ev.text}")
        await sender
    if t.t_last_audio and last_final_at:
        print(f"\nlatency last-audio-byte -> last final: {(last_final_at - t.t_last_audio) * 1000:.0f} ms")
    else:
        print("\nno final transcript received")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wav", help="16 kHz mono 16-bit WAV to stream to Voice Transcribe")
    ap.add_argument("--mode", default="DIARIZATION", choices=["DIARIZATION", "ENDPOINTING", "PUSH_TO_TALK"])
    ap.add_argument("--model", default=None, help="chat model id (default: muse-spark-1.3)")
    ap.add_argument("--skip-chat", action="store_true")
    args = ap.parse_args()

    if not api_key_from_env():
        sys.exit("No API key found. Set LLAMA_API_KEY (or MODEL_API_KEY) to your Meta Model API key\n"
                 "  (create one at https://dev.meta.ai ; keys look like LLM_<id>_<secret>).")
    print("base url:", base_url_from_env())
    try:
        if not args.skip_chat:
            asyncio.run(smoke_chat(args.model or DEFAULT_CHAT_MODEL))
        if args.wav:
            asyncio.run(smoke_transcribe(args.wav, args.mode))
    except MuseError as e:
        sys.exit(f"Muse error: {e}")


if __name__ == "__main__":
    main()
