# Common Ground

**Real-time coaching for the moments when words stop meaning the same thing.**

A communication copilot for Ray-Ban Meta glasses, built for the Meta Global AI Developer Hackathon
(Oct 30 – Nov 4, 2026). It listens to a two-person conversation, detects observable evidence that the
two people have stopped meaning the same thing, whispers one short repair into the wearer's ear, and
then checks whether the next exchange actually improved.

It is explicitly **not** an emotion meter. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the
principles and [docs/WEARABLES_SDK.md](docs/WEARABLES_SDK.md) for the glasses integration.

## Layout

```
backend/   Python: FastAPI WebSocket server, Muse clients, signal models, coaching engine, eval scripts
ios/       SwiftUI companion app (glasses mic/speaker via Meta Wearables Device Access Toolkit)
fixtures/  Scenario fixtures (hand-authored + Muse-generated) and matched-tone audio
docs/      Architecture, Muse API notes, wearables SDK spike checklist
```

## Quick start (backend, no glasses needed)

```bash
cd backend
pip install -e ".[dev]"           # add ,audio for wav2vec2 speech emotion (needs torch)
pytest -q
python scripts/eval_scenarios.py              # offline loop: rules + template interpreter
export LLAMA_API_KEY=...                      # Meta Muse API key
python scripts/smoke_muse.py                  # verifies the key, prints cost per call
python scripts/eval_scenarios.py --muse       # two-tier loop: fixed cues + Muse Spark when evidence accumulates
python scripts/eval_scenarios.py --muse --realtime --partials 2   # paced replay: Tier 1 timing incl. speculative start
python scripts/eval_scenarios.py --muse --tier1-always            # ablation: Muse on every nomination
uvicorn common_ground.server.app:app --port 8000
python scripts/replay_session.py ../fixtures/scenarios/polite_confusion_01.json   # what the phone sees
```

## How it works, in one scenario

```
wearer:      We integrate with your existing workflow. Your team keeps the same tools.
counterpart: Yeah, that makes sense.
wearer:      Great. Rollout takes about two weeks.
counterpart: Sure. So would we have to replace everything we have now?
                 ↳ rules: agreement words + clarification question  → nominate POLITE_CONFUSION
                 ↳ earpiece, ~0.7 s later (Tier 0, fixed vocabulary): "Ask them to restate"
                 ↳ evidence accumulator: first sign of trouble → not yet worth a model call
                 ... if the pattern sustains (or a cue fails), Tier 1 runs in the background:
                 ↳ Muse Spark, streamed: "integration" likely heard as "replacement"
                 ↳ earpiece at the next pause: "Check their picture"
                 ↳ phone: "Say what stays unchanged, then ask if that matches what they heard."
counterpart: Oh, so it sits next to what we already run. Fine.
                 ↳ outcome: RESOLVED (success test met on the next turn)
```

## Evaluation

`scripts/eval_scenarios.py` replays the fixtures through the real pipeline and reports false-intervention
rate on neutral conversations, detection rate on seeded mismatches, hypothesis-type accuracy (e.g. never
calling disagreement "confusion"), forbidden-strategy violations, and interpreter latency p50/p95.
Run it with and without `--muse` to get the rules-only vs Muse ablation.

## Privacy and acceptable use

Consent-first by construction: a session cannot start without `consent_confirmed`. Raw audio is never
written to disk; only turn transcripts and coaching events are kept, as JSONL in `runs/`. Muse is used in
Standard (non-Contributor) mode. The coach optimises for mutual understanding and voluntary agreement;
it never claims to detect deception or hidden malice.
