#!/usr/bin/env python3
"""Replay scenario fixtures through the full coach loop and score it.

  python scripts/eval_scenarios.py                 # offline: rules + FallbackInterpreter
  python scripts/eval_scenarios.py --muse          # rules + Muse Spark interpreter (needs LLAMA_API_KEY)
  python scripts/eval_scenarios.py --no-text-emotion   # skip the RoBERTa model (faster startup)
  python scripts/eval_scenarios.py --out runs/eval.json

Metrics: false intervention rate on neutral scenarios, seeded-issue detection rate, hypothesis-type
accuracy, forbidden-type/strategy violations, cue timing, and interpreter latency p50/p95.
"""
from __future__ import annotations

import argparse, asyncio, json, os, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common_ground.schema import SessionConfig, SessionGoal
from common_ground.engine import Coach, ConversationGraph
from common_ground.engine.fallback import FallbackInterpreter
from common_ground.engine.interpreter import Interpreter
from common_ground.engine.pipeline import Pipeline
from common_ground.signals import LinguisticDetector

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "scenarios"


async def run_one(sc: dict, interp, text_emotion, min_gap: float) -> dict:
    cfg = SessionConfig(session_id=sc["id"], goal=SessionGoal(sc.get("goal", "explain")),
                        intended_message=sc.get("intended_message"), min_seconds_between_cues=min_gap,
                        consent_confirmed=True)
    cues: list[dict] = []
    async def emit(kind, payload):
        if kind == "cue":
            cues.append(payload)
    # Fixture times are conversation time; use a fake clock so throttling follows the script, not wall time.
    clock = {"t": 0.0}
    coach = Coach(cfg, ConversationGraph(sc["id"]), interp, emit=emit, clock=lambda: clock["t"], stale_after_s=60)
    pipe = Pipeline(coach, LinguisticDetector(), text_emotion)
    t0 = time.monotonic()
    for i, t in enumerate(sc["turns"]):
        clock["t"] = t["t_end"]
        await pipe.on_text_turn(t["speaker"], t["text"], t["t_start"], t["t_end"])
    wall = time.monotonic() - t0
    ex = sc["expect"]
    first = cues[0] if cues else None
    res = {"id": sc["id"], "type": sc["type"], "intervened": bool(cues), "n_cues": len(cues),
           "first_turn": first["turn_id"] if first else None,
           "hyp_type": first["hypothesis"]["type"] if first else None,
           "strategy": first["strategy"] if first else None,
           "cue": first["earpiece_cue"] if first else None,
           "suggestion": first["suggestion"] if first else None,
           "wall_s": round(wall, 3), "metrics": coach.metrics.summary(), "failures": []}
    want = ex.get("should_intervene")
    if want is False and cues:
        res["failures"].append("false_intervention")
    if want is True and not cues:
        res["failures"].append("missed_seeded_issue")
    if first:
        ok_turns = ex.get("intervene_on_turn_in") or ([ex["intervene_on_turn"]] if "intervene_on_turn" in ex else None)
        if ok_turns and first["turn_id"] not in ok_turns:
            res["failures"].append(f"wrong_turn:{first['turn_id']} not in {ok_turns}")
        if ex.get("mismatch_type") and res["hyp_type"] not in ex["mismatch_type"]:
            res["failures"].append(f"wrong_type:{res['hyp_type']}")
        if res["hyp_type"] in ex.get("must_not_be", []):
            res["failures"].append(f"forbidden_type:{res['hyp_type']}")
        if ex.get("strategy_in") and res["strategy"] not in ex["strategy_in"]:
            res["failures"].append(f"strategy_not_allowed:{res['strategy']}")
        if res["strategy"] in ex.get("strategy_not_in", []):
            res["failures"].append(f"forbidden_strategy:{res['strategy']}")
    if sc["type"] == "tone_dependent" and ex.get("text_only_should_intervene") is False and cues:
        res["failures"].append("text_only_intervened_on_tone_dependent")
    return res


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--muse", action="store_true")
    ap.add_argument("--no-text-emotion", action="store_true")
    ap.add_argument("--min-gap", type=float, default=0.0, help="min seconds between cues (0 = score every nomination)")
    ap.add_argument("--only", default=None)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    if a.muse:
        if not os.environ.get("LLAMA_API_KEY"):
            sys.exit("--muse requires LLAMA_API_KEY")
        from common_ground.muse import MuseChat
        interp = Interpreter(MuseChat())
    else:
        interp = FallbackInterpreter()
    te = None
    if not a.no_text_emotion:
        try:
            from common_ground.signals import TextEmotionClassifier
            te = TextEmotionClassifier(); te.warmup()
        except Exception as e:
            print(f"[warn] text emotion unavailable: {e}")

    files = sorted(FIX.glob("*.json"))
    if a.only:
        files = [f for f in files if a.only in f.name]
    results = [await run_one(json.loads(f.read_text()), interp, te, a.min_gap) for f in files]

    neutral = [r for r in results if r["type"] == "neutral"]
    seeded = [r for r in results if r["type"] not in ("neutral", "tone_dependent", "genuine_disagreement")]
    summary = {
        "interpreter": "muse" if a.muse else "fallback",
        "n": len(results),
        "false_intervention_rate": round(sum(r["intervened"] for r in neutral) / max(1, len(neutral)), 3),
        "seeded_detection_rate": round(sum(r["intervened"] for r in seeded) / max(1, len(seeded)), 3),
        "type_accuracy": round(sum(not any(f.startswith("wrong_type") or f.startswith("forbidden_type") for f in r["failures"])
                                   for r in results if r["intervened"]) / max(1, sum(r["intervened"] for r in results)), 3),
        "scenarios_with_failures": sum(bool(r["failures"]) for r in results),
        "interpret_p50_s": _pct([l for r in results for l in r["metrics"].get("interpret_latency_s", [])] or
                                [r["metrics"]["interpret_p50_s"] for r in results if r["metrics"]["interpret_p50_s"]], 0.5),
        "interpret_p95_s": _pct([r["metrics"]["interpret_p95_s"] for r in results if r["metrics"]["interpret_p95_s"]], 0.95),
    }
    w = max(len(r["id"]) for r in results)
    for r in results:
        flag = "OK " if not r["failures"] else "XX "
        cue = f'{r["hyp_type"]} / {r["strategy"]} @#{r["first_turn"]}: "{r["cue"]}"' if r["intervened"] else "silent"
        print(f'{flag}{r["id"]:<{w}}  {cue}' + (f'   <- {", ".join(r["failures"])}' if r["failures"] else ""))
    print("\n" + json.dumps(summary, indent=2))
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps({"summary": summary, "results": results}, indent=2))


def _pct(xs, q):
    xs = sorted(x for x in xs if x is not None)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))], 3) if xs else None


if __name__ == "__main__":
    asyncio.run(main())
