#!/usr/bin/env python3
"""Drive a running server from a scenario fixture over the WebSocket, printing what the phone would see.

  uvicorn common_ground.server.app:app --port 8000 &
  python scripts/replay_session.py ../fixtures/scenarios/polite_confusion_01.json [--url ws://localhost:8000/ws/session] [--realtime]
"""
from __future__ import annotations

import argparse, asyncio, json, sys
from pathlib import Path

import websockets


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("fixture")
    ap.add_argument("--url", default="ws://localhost:8000/ws/session")
    ap.add_argument("--realtime", action="store_true")
    a = ap.parse_args()
    sc = json.loads(Path(a.fixture).read_text())
    async with websockets.connect(a.url) as ws:
        async def listen():
            async for raw in ws:
                m = json.loads(raw)
                p = m["payload"]
                if m["type"] == "cue":
                    print(f'\n  >>> EARPIECE: "{p["earpiece_cue"]}"\n      phone: {p["suggestion"]}\n      because: {p["hypothesis"]["summary"]}\n')
                elif m["type"] == "state":
                    print(f'  [state] {p}')
                elif m["type"] == "metrics":
                    print(json.dumps(p, indent=2)); return
        task = asyncio.create_task(listen())
        await ws.send(json.dumps({"type": "start", "config": {"session_id": sc["id"], "goal": sc.get("goal", "explain"),
                                  "intended_message": sc.get("intended_message"), "consent_confirmed": True,
                                  "min_seconds_between_cues": 0}}))
        last = 0.0
        for t in sc["turns"]:
            if a.realtime:
                await asyncio.sleep(max(0, t["t_start"] - last)); last = t["t_end"]
            print(f'{t["speaker"]:>11}: {t["text"]}')
            await ws.send(json.dumps({"type": "text_turn", "text_turn": t}))
            await asyncio.sleep(0.05)
        await asyncio.sleep(1.0)
        await ws.send(json.dumps({"type": "stop"}))
        await task


if __name__ == "__main__":
    asyncio.run(main())
