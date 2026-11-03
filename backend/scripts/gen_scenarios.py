#!/usr/bin/env python3
"""Generate additional scenario fixtures with Muse Spark (requires LLAMA_API_KEY).

  python scripts/gen_scenarios.py --per-type 3 --out ../fixtures/scenarios/generated

Each generated file carries "generated": true and the same `expect` block shape as the hand-authored
fixtures, authored by the model from the type's definition. They are test fixtures, not ground truth,
and are never mixed into the hand-authored set. Cost: ~1.5k tokens per scenario.
"""
from __future__ import annotations

import argparse, asyncio, json, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TYPES = {
    "polite_confusion": ("explain", "counterpart says agreement words ('yeah, makes sense') then asks a question "
                         "that reveals they hold a different mental model", True, ["polite_confusion", "term_ambiguity"]),
    "term_ambiguity": ("explain", "wearer uses one technical/ambiguous term; counterpart queries it", True, ["term_ambiguity"]),
    "indirect_refusal": ("negotiate", "wearer asks for a commitment; counterpart defers softly ('that might be hard', "
                         "'let me think') without saying no", True, ["indirect_refusal"]),
    "escalating_friction": ("resolve", "counterpart asks the same question 2-3 times while the wearer re-explains "
                            "at length; counterpart answers get shorter", True, ["escalating_friction", "unanswered_objection"]),
    "genuine_disagreement": ("negotiate", "counterpart clearly understands and clearly disagrees, politely", "either",
                             ["genuine_disagreement", "none"]),
    "neutral": ("understand", "ordinary, friendly, fully successful exchange with no breakdown", False, []),
}

PROMPT = """Write a realistic spoken two-person conversation of {n} turns alternating wearer/counterpart,
in a {setting} setting. Scenario type: {typ}: {desc}. Return JSON:
{{"intended_message": str, "turns": [{{"speaker": "wearer"|"counterpart", "text": str}}, ...],
  "seeded_turn": int (index of the counterpart turn where the issue first becomes observable, or -1)}}
Natural, terse, spoken English. No stage directions. Vary names, domains and register."""

SETTINGS = ["software vendor sales call", "landlord and tenant", "two co-founders", "doctor and patient",
            "contractor and homeowner", "manager and report", "two friends planning a trip", "customer support"]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-type", type=int, default=2)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[2] / "fixtures/scenarios/generated"))
    a = ap.parse_args()
    if not os.environ.get("LLAMA_API_KEY"):
        sys.exit("LLAMA_API_KEY required")
    from common_ground.muse import MuseChat
    chat = MuseChat()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    k = 0
    for typ, (goal, desc, should, types) in TYPES.items():
        for i in range(a.per_type):
            setting = SETTINGS[(k + i) % len(SETTINGS)]
            n = 4 if typ != "escalating_friction" else 6
            j = await chat.complete_json([{"role": "user", "content": PROMPT.format(n=n, setting=setting, typ=typ, desc=desc)}],
                                         max_tokens=700, temperature=0.9)
            turns, t = [], 0.0
            for tr in j["turns"]:
                dur = max(1.0, 0.4 * len(tr["text"].split()))
                turns.append({"speaker": tr["speaker"], "text": tr["text"], "t_start": round(t, 1), "t_end": round(t + dur, 1)})
                t += dur + 0.5
            sc = {"id": f"gen_{typ}_{i:02d}", "type": typ, "goal": goal, "generated": True, "setting": setting,
                  "intended_message": j.get("intended_message"), "turns": turns,
                  "expect": {"should_intervene": should, "mismatch_type": types,
                             **({"intervene_on_turn_in": [j["seeded_turn"], j["seeded_turn"] + 2]} if j.get("seeded_turn", -1) >= 0 else {})}}
            (out / f"{sc['id']}.json").write_text(json.dumps(sc, indent=2) + "\n")
            k += 1
            print("wrote", sc["id"], "| usage:", getattr(chat, "usage", None))


if __name__ == "__main__":
    asyncio.run(main())
