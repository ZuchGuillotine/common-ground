"""Latency bench for the perception tier. Run: python backend/scripts/bench_signals.py [--speech]

Prints load time and per-call latency (median / p95 / max) for each component on this CPU.
"""
from __future__ import annotations

import argparse
import os
import statistics
import time
from collections.abc import Callable

import numpy as np

from common_ground.schema import Turn
from common_ground.signals import (
    AcousticExtractor,
    LinguisticDetector,
    TextEmotionClassifier,
)


def timeit(name: str, fn: Callable[[], object], n: int) -> None:
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1000)
    ts.sort()
    print(f"{name:<34} median {statistics.median(ts):8.2f} ms   p95 {ts[int(0.95 * (n - 1))]:8.2f} ms   max {ts[-1]:8.2f} ms   (n={n})")


def vowel(f0: float, dur: float, sr: int = 16000) -> np.ndarray:
    t = np.arange(int(dur * sr)) / sr
    y = sum((1.0 / k) * np.sin(2 * np.pi * f0 * k * t) for k in range(1, 8))
    y *= 0.55 + 0.45 * np.sin(2 * np.pi * 4 * t)
    return (0.3 * y / np.abs(y).max() * 32767).astype(np.int16)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--speech", action="store_true", help="also bench the torch speech-emotion model")
    ap.add_argument("-n", type=int, default=50)
    args = ap.parse_args()
    print(f"cpu cores: {os.cpu_count()}\n")

    ling = LinguisticDetector()
    history = [
        Turn(turn_id=i, speaker="counterpart" if i % 2 else "wearer", text=f"Do we need the {w} layer at all?",
             t_start=i, t_end=i + 1)
        for i, w in enumerate(["SSO", "Gateway", "schema", "endpoint", "webhook", "tenant"] * 5)
    ]
    text = "Yeah, that makes sense. So would we have to replace everything?"
    timeit("linguistic (30-turn history)", lambda: ling.analyze(text, "counterpart", history), args.n * 10)

    te = TextEmotionClassifier()
    t0 = time.perf_counter()
    te.warmup()
    print(f"{'text emotion load+warmup':<34} {(time.perf_counter() - t0) * 1000:8.0f} ms")
    for label, s in [("short", "Sure, I guess."), ("medium", text),
                     ("long", " ".join([text] * 4))]:
        timeit(f"text emotion ({label}, {len(s.split())} words)", lambda s=s: te.classify(s), args.n)

    ex = AcousticExtractor()
    for dur in (2.0, 4.0):
        x = vowel(180, dur)
        timeit(f"acoustic features ({dur:.0f} s window)", lambda x=x: ex.features(x, speaker="w"), args.n)

    if args.speech:
        from common_ground.signals import SpeechEmotionClassifier
        se = SpeechEmotionClassifier()
        t0 = time.perf_counter()
        se.warmup()
        print(f"{'speech emotion load+warmup':<34} {(time.perf_counter() - t0) * 1000:8.0f} ms")
        x = vowel(180, 3.0)
        timeit("speech emotion (3 s window)", lambda: se.classify(x), max(5, args.n // 10))


if __name__ == "__main__":
    main()
