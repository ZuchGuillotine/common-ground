# Run sheet: final day (deadline Wed Nov 4, 9:00 PM ET)

Order matters. Do not skip ahead; each block has a hard stop.

## Block 0 (15 min): confirm Muse works from your machine
```bash
cd backend && pip install -e ".[dev]" && export LLAMA_API_KEY=...
python scripts/smoke_muse.py                      # key valid? cost per call?
python scripts/smoke_muse.py --wav ../fixtures/audio/<any 16k mono>.wav   # Voice Transcribe latency
python scripts/eval_scenarios.py --muse --out runs/eval_muse.json
python scripts/eval_scenarios.py        --out runs/eval_rules.json        # the ablation
```
If `smoke_muse.py` fails on protocol details, fix the constants at the top of `backend/common_ground/muse/transcribe.py`
(see docs/MUSE_API.md for what was documented vs guessed).

## Block 1 (2 h hard stop): glasses mic spike
Follow docs/WEARABLES_SDK.md. Goal: 16 kHz PCM from the glasses arriving at `ws://<laptop>:8000/ws/session`
as binary frames and a diarized transcript printing on the server. **At 2 h, if not working, switch
`AudioSource` to `PhoneMicSource` and keep the glasses as Bluetooth output only.** That is still a wearable demo.

## Block 2 (1 h): one real misunderstanding, end to end
Two people, scripted from `fixtures/scenarios/polite_confusion_01.json`, live. Watch for: speaker map
correct (fix with `assign_speaker`), cue arrives before the wearer's next turn, cue echo doesn't produce
a ghost turn (check `coaching_overlap`). Record screen + audio for the demo video.

## Block 3 (1 h): matched-tone audio
Record `fixtures/audio/same_words_01_{warm,flat,irritated}.wav` (the counterpart line "Sure, that's fine"
spoken three ways, 16 kHz mono). Run them through `scripts/bench_signals.py --wav` and show that prosody
deltas differ while the text signals are identical, and that the coach stays silent on text alone.

## Block 4 (45 min): numbers for the submission
Fill the table in docs/RESULTS.md from `runs/eval_*.json` and the live latency log (`metrics` message on stop).

## Block 5 (1 h): demo video + submission
3-minute video: the problem (30 s), live demo with the glasses (90 s), the eval table and the
rules-vs-Muse ablation (45 s), limitations and consent (15 s). Submit by 8 PM ET, not 8:59.
