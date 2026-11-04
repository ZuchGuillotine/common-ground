#!/usr/bin/env python3
"""Replay scenario fixtures through the full coach loop and score it.

  python scripts/eval_scenarios.py                 # offline: rules + FallbackInterpreter
  python scripts/eval_scenarios.py --muse          # rules + Muse Spark interpreter (needs LLAMA_API_KEY)
  python scripts/eval_scenarios.py --no-text-emotion   # skip the RoBERTa model (faster startup)
  python scripts/eval_scenarios.py --out runs/eval.json
  python scripts/eval_scenarios.py --muse --partials 2 --realtime   # speculative Tier 1 on simulated partials, paced
  python scripts/eval_scenarios.py --muse --tier1-always            # ablation: Muse on every nomination

Metrics: false intervention rate on neutral scenarios, seeded-issue detection rate, hypothesis-type
accuracy, forbidden-type/strategy violations, cue timing, and Tier 1 (Muse) latency: full call p50/p95,
time to the early act/cue/how fields, and cue-ready time relative to the trigger turn's end.
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


def _partials_of(text: str, n: int) -> list[str]:
    """n cumulative word-prefix partials of a turn, at k/(n+1) of the words (like MockMuseTranscribe)."""
    words = text.split()
    cuts = sorted({len(words) * k // (n + 1) for k in range(1, n + 1)} - {0, len(words)})
    return [" ".join(words[:c]) for c in cuts]


async def run_one(sc: dict, interp, text_emotion, a) -> dict:
    min_gap = a.min_gap
    cfg = SessionConfig(session_id=sc["id"], goal=SessionGoal(sc.get("goal", "explain")),
                        intended_message=sc.get("intended_message"), min_seconds_between_cues=min_gap,
                        consent_confirmed=True)
    cues: list[dict] = []
    details: list[dict] = []
    async def emit(kind, payload):
        if kind == "cue":
            cues.append(payload)
        elif kind == "cue_detail":
            details.append(payload)
    # Fixture times are conversation time; use a fake clock so throttling follows the script, not wall time.
    clock = {"t": 0.0}
    coach = Coach(cfg, ConversationGraph(sc["id"]), interp, emit=emit, clock=lambda: clock["t"],
                  stale_after_s=a.stale_after, tier1_always=a.tier1_always, speculate=a.partials > 0)
    pipe = Pipeline(coach, LinguisticDetector(), text_emotion)
    t0 = time.monotonic()
    sim_t = 0.0
    async def pace(t):   # --realtime: sleep until conversation time t (scaled by --speed)
        nonlocal sim_t
        if a.realtime and t > sim_t:
            await asyncio.sleep((t - sim_t) / a.speed)
        sim_t = max(sim_t, t)
    for i, t in enumerate(sc["turns"]):
        if a.partials and t["speaker"] == "counterpart":
            parts = _partials_of(t["text"], a.partials)
            for k, ptxt in enumerate(parts, 1):
                t_now = t["t_start"] + (t["t_end"] - t["t_start"]) * k / (len(parts) + 1)
                await pace(t_now); clock["t"] = t_now
                await pipe.on_partial_text("counterpart", ptxt, t["t_start"], t_now)
        await pace(t["t_end"]); clock["t"] = t["t_end"]
        await pipe.on_text_turn(t["speaker"], t["text"], t["t_start"], t["t_end"])
        if not a.realtime:
            # Unpaced replay measures QUALITY: let Tier 1 finish before the scripted clock jumps to the next
            # turn (a live session's 5 s call spans real time; here the next turn would arrive "instantly").
            await coach.wait_background()
    if a.realtime:   # paced replay measures TIMING: give a trailing Tier 1 its real chance to land
        await pace(sc["turns"][-1]["t_end"] + a.stale_after)
    await coach.drain()
    wall = time.monotonic() - t0
    ex = sc["expect"]
    tier0 = [c for c in cues if c.get("tier", 0) == 0]
    tier1 = [c for c in cues if c.get("tier") == 1]
    first = cues[0] if cues else None
    # type/strategy are scored at the FIRST intervention moment: the Tier 1 (model) judgement when it cued on
    # that same turn (its full hypothesis arrives in cue_detail), else the Tier 0 (rules) nomination.
    judged = first
    if first and tier1 and tier1[0]["turn_id"] == first["turn_id"]:
        judged = dict(tier1[0])
        det = next((d for d in details if d["turn_id"] == first["turn_id"]), None)
        if det:
            judged["hypothesis"], judged["strategy"] = det["hypothesis"], det["strategy"]
    m = coach.metrics
    res = {"id": sc["id"], "type": sc["type"], "intervened": bool(cues), "n_cues": len(cues),
           "first_turn": first["turn_id"] if first else None,
           "hyp_type": judged["hypothesis"]["type"] if judged else None,
           "strategy": judged["strategy"] if judged else None,
           "cue": first["earpiece_cue"] if first else None,
           "suggestion": first["suggestion"] if first else None,
           "tier1_cue": tier1[0]["earpiece_cue"] if tier1 else None,
           "tier1_suggestion": (details[-1]["suggestion"] if details else (tier1[0]["suggestion"] if tier1 else None)),
           "tier1_calls": m.tier1_calls, "tier1_silent": m.tier1_silent, "tier1_dropped": m.tier1_dropped,
           "speculative": [m.speculative_started, m.speculative_kept, m.speculative_discarded],
           "interpret_latency_s": [round(x, 3) for x in m.interpret_latency_s],
           "tier1_early_s": [round(x, 3) for x in m.tier1_early_s],
           "tier1_ready_after_turn_s": [round(x, 3) for x in m.tier1_ready_after_turn_s],
           "wall_s": round(wall, 3), "metrics": m.summary(), "failures": []}
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
    ap.add_argument("--dir", default=None, help="scenario directory (default: fixtures/scenarios; "
                                                "e.g. fixtures/scenarios/generated for the held-out set)")
    ap.add_argument("--partials", type=int, default=0, help="simulate N partial transcripts per counterpart turn "
                                                            "(enables speculative Tier 1)")
    ap.add_argument("--realtime", action="store_true", help="pace turns by fixture time so latency numbers are meaningful")
    ap.add_argument("--speed", type=float, default=1.0, help="with --realtime: playback speed factor")
    ap.add_argument("--stale-after", type=float, default=None,
                    help="Tier 1 cues older than this are dropped (default: 8 s with --realtime, else 60 s)")
    ap.add_argument("--tier1-always", action="store_true", help="ablation: run the interpreter on every nomination")
    a = ap.parse_args()
    if a.stale_after is None:
        a.stale_after = 8.0 if a.realtime else 60.0

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

    files = sorted((Path(a.dir) if a.dir else FIX).glob("*.json"))
    if a.only:
        files = [f for f in files if a.only in f.name]
    results = [await run_one(json.loads(f.read_text()), interp, te, a) for f in files]
    lat = [x for r in results for x in r["interpret_latency_s"]]
    early = [x for r in results for x in r["tier1_early_s"]]
    ready = [x for r in results for x in r["tier1_ready_after_turn_s"]]

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
        "tier0_cues": sum(r["metrics"]["tier0_cues"] for r in results),
        "tier1_calls": sum(r["tier1_calls"] for r in results),
        "tier1_cues": sum(r["metrics"]["tier1_cues"] for r in results),
        "tier1_silent": sum(r["tier1_silent"] for r in results),
        "tier1_dropped": sum(r["tier1_dropped"] for r in results),
        "speculative_started_kept_discarded": [sum(r["speculative"][i] for r in results) for i in range(3)],
        "interpret_p50_s": _pct(lat, 0.5),
        "interpret_p95_s": _pct(lat, 0.95),
        "tier1_early_p50_s": _pct(early, 0.5),
        "tier1_ready_after_turn_p50_s": _pct(ready, 0.5),
        "tier1_ready_after_turn_p95_s": _pct(ready, 0.95),
    }
    w = max(len(r["id"]) for r in results)
    for r in results:
        flag = "OK " if not r["failures"] else "XX "
        cue = f'{r["hyp_type"]} / {r["strategy"]} @#{r["first_turn"]}: "{r["cue"]}"' if r["intervened"] else "silent"
        t1 = ""
        if r["tier1_calls"]:
            t1 = f'  | T1 {r["tier1_calls"]}x'
            t1 += f' "{r["tier1_cue"]}"' if r["tier1_cue"] else (" silent" if r["tier1_silent"] else " dropped")
            if r["tier1_ready_after_turn_s"]:
                t1 += f' ready {r["tier1_ready_after_turn_s"][0]:+.2f}s'
        print(f'{flag}{r["id"]:<{w}}  {cue}{t1}' + (f'   <- {", ".join(r["failures"])}' if r["failures"] else ""))
    if a.muse:   # UsageMeter: tokens and estimated USD for this run
        u = interp.chat.usage
        summary["muse_usage"] = {"calls": u.calls, "input_tokens": u.input_tokens,
                                 "output_tokens": u.output_tokens, "cost_usd": round(u.cost_usd, 5)}
    print("\n" + json.dumps(summary, indent=2))
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps({"summary": summary, "results": results}, indent=2))


def _pct(xs, q):
    xs = sorted(x for x in xs if x is not None)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))], 3) if xs else None


if __name__ == "__main__":
    asyncio.run(main())
