# Scenario fixtures

Hand-authored test conversations, one JSON per file. `expect` encodes what a good coach does:

| field | meaning |
|---|---|
| `should_intervene` | `true`, `false`, or `"either"` (acceptable both ways; scored on type/strategy only) |
| `mismatch_type` | acceptable hypothesis types |
| `must_not_be` | hypothesis types that count as a failure (e.g. calling disagreement "confusion") |
| `intervene_on_turn` / `intervene_on_turn_in` | the turn index(es) where the first cue is acceptable |
| `strategy_in` / `strategy_not_in` | acceptable / forbidden strategies |

These are **test fixtures, not ground truth about emotion**. `scripts/gen_scenarios.py` can add
Muse-generated variants (kept in `generated/`, never mixed with the hand-authored set).
