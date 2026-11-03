"""Numpy-only prosody features for 2-4 s windows of 16 kHz mono PCM16.

Nothing here is a verdict: these are cheap, per-window measurements normalised against the same
speaker's running baseline. On silence every optional field is None and pause_ratio is 1.0.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Union

import numpy as np

from common_ground.schema import AcousticFeatures

PcmInput = Union[np.ndarray, bytes, bytearray, memoryview]

_EPS = 1e-9
_SILENCE_DBFS = -60.0     # below this the whole window counts as silence


def to_float(pcm16: PcmInput) -> np.ndarray:
    """PCM16 (int16 array or raw little-endian bytes) -> float32 in [-1, 1]. Float input passes through."""
    if isinstance(pcm16, (bytes, bytearray, memoryview)):
        x = np.frombuffer(pcm16, dtype="<i2")
    else:
        x = np.asarray(pcm16)
    if x.dtype.kind == "f":
        return x.astype(np.float32, copy=False)
    return x.astype(np.float32) / 32768.0


def _frames(x: np.ndarray, win: int, hop: int) -> np.ndarray:
    """(n_frames, win) view; zero-pads the tail so even short windows yield >= 1 frame."""
    if len(x) < win:
        x = np.pad(x, (0, win - len(x)))
    n = 1 + (len(x) - win) // hop
    idx = np.arange(win)[None, :] + hop * np.arange(n)[:, None]
    return x[idx]


def _db(rms: np.ndarray | float) -> np.ndarray | float:
    return 20.0 * np.log10(np.maximum(rms, _EPS))


def hz_to_semitones(hz: float) -> float:
    return 12.0 * math.log2(hz / 440.0)


@dataclass
class SpeakerBaseline:
    """Exponential moving average of a speaker's pitch (semitones) and level (dB)."""

    alpha: float = 0.2
    n: int = 0
    pitch_st: Optional[float] = None
    rms_db: Optional[float] = None

    def deltas(self, pitch_hz: Optional[float], rms_db: Optional[float]) -> tuple[Optional[float], Optional[float]]:
        dp = hz_to_semitones(pitch_hz) - self.pitch_st if pitch_hz and self.pitch_st is not None else None
        de = rms_db - self.rms_db if rms_db is not None and self.rms_db is not None else None
        return dp, de

    def update(self, pitch_hz: Optional[float], rms_db: Optional[float]) -> None:
        if pitch_hz:
            st = hz_to_semitones(pitch_hz)
            self.pitch_st = st if self.pitch_st is None else (1 - self.alpha) * self.pitch_st + self.alpha * st
        if rms_db is not None:
            self.rms_db = rms_db if self.rms_db is None else (1 - self.alpha) * self.rms_db + self.alpha * rms_db
        self.n += 1


@dataclass
class AcousticExtractor:
    frame_ms: float = 25.0
    hop_ms: float = 10.0
    pitch_win_ms: float = 40.0
    f0_min: float = 60.0
    f0_max: float = 400.0
    voicing_threshold: float = 0.6     # normalised autocorrelation peak needed to accept a pitch
    vad_dynamic_db: float = 30.0       # frames quieter than (peak - this) are pauses
    vad_floor_dbfs: float = -50.0      # ...and anything below this is always a pause
    min_syllable_gap_ms: float = 120.0
    baseline_alpha: float = 0.2
    baselines: dict[str, SpeakerBaseline] = field(default_factory=dict)

    # ------------------------------------------------------------------ public
    def features(self, pcm16: PcmInput, sr: int = 16000, speaker: str = "unknown") -> AcousticFeatures:
        x = to_float(pcm16)
        duration = len(x) / sr if sr > 0 else 0.0
        if len(x) == 0:
            return AcousticFeatures(duration_s=0.0, pause_ratio=1.0)

        hop = max(1, int(sr * self.hop_ms / 1000))
        win = max(hop, int(sr * self.frame_ms / 1000))
        fr = _frames(x, win, hop)
        frame_db = np.asarray(_db(np.sqrt(np.mean(fr * fr, axis=1))))
        peak_db = float(frame_db.max())

        if peak_db < _SILENCE_DBFS:
            return AcousticFeatures(duration_s=duration, pause_ratio=1.0, leading_pause_s=duration)

        speech = frame_db > max(self.vad_floor_dbfs, peak_db - self.vad_dynamic_db)
        pause_ratio = 1.0 - float(speech.mean())
        first = int(np.argmax(speech)) if speech.any() else len(speech)
        leading_pause = first * hop / sr

        rms_db = float(_db(np.sqrt(np.mean(x[speech_mask(speech, hop, win, len(x))] ** 2)))) if speech.any() else None
        f0_mean, f0_std = self._pitch(x, sr, hop, speech)
        rate = self._speech_rate(frame_db, speech, hop, sr)

        base = self.baselines.setdefault(speaker, SpeakerBaseline(alpha=self.baseline_alpha))
        d_pitch, d_energy = base.deltas(f0_mean, rms_db)
        base.update(f0_mean, rms_db)

        return AcousticFeatures(
            duration_s=duration,
            rms_db=rms_db,
            pitch_hz_mean=f0_mean,
            pitch_hz_std=f0_std,
            pitch_delta_vs_baseline=d_pitch,
            energy_delta_vs_baseline=d_energy,
            speech_rate_sps=rate,
            pause_ratio=pause_ratio,
            leading_pause_s=leading_pause,
        )

    # ----------------------------------------------------------------- private
    def _pitch(self, x: np.ndarray, sr: int, hop: int, speech: np.ndarray) -> tuple[Optional[float], Optional[float]]:
        """Autocorrelation pitch on speech frames; sub-harmonic errors suppressed by preferring the
        shortest lag whose normalised autocorrelation is within 10% of the best peak."""
        win = int(sr * self.pitch_win_ms / 1000)
        if len(x) < win:
            return None, None
        fr = _frames(x, win, hop)
        n = min(len(fr), len(speech))
        fr = fr[:n][speech[:n]]
        if len(fr) == 0:
            return None, None
        fr = fr - fr.mean(axis=1, keepdims=True)
        fr = fr * np.hanning(win)[None, :]
        nfft = 1 << (2 * win - 1).bit_length()
        spec = np.fft.rfft(fr, nfft, axis=1)
        ac = np.fft.irfft(spec * np.conj(spec), nfft, axis=1)[:, :win]
        ac = ac / np.maximum(ac[:, :1], _EPS)

        lo, hi = int(sr / self.f0_max), min(int(sr / self.f0_min), win - 2)
        if hi <= lo + 1:
            return None, None
        seg = ac[:, lo:hi]
        best = seg.max(axis=1)
        ok = best >= self.voicing_threshold
        if ok.sum() < 3:
            return None, None
        # shortest lag that is a local maximum and >= 0.9 * best  ->  avoids octave-down errors
        is_peak = (seg[:, 1:-1] > seg[:, :-2]) & (seg[:, 1:-1] >= seg[:, 2:])
        cand = is_peak & (seg[:, 1:-1] >= 0.9 * best[:, None])
        lag_idx = np.where(cand.any(axis=1), cand.argmax(axis=1) + 1, seg.argmax(axis=1))
        lags = (lag_idx + lo).astype(np.float64)
        # parabolic refinement
        i = np.arange(len(ac))
        li = lag_idx + lo
        y0, y1, y2 = ac[i, li - 1], ac[i, li], ac[i, li + 1]
        denom = y0 - 2 * y1 + y2
        shift = np.where(np.abs(denom) > _EPS, 0.5 * (y0 - y2) / np.where(np.abs(denom) > _EPS, denom, 1), 0.0)
        lags = lags + np.clip(shift, -1, 1)
        f0 = (sr / lags)[ok]
        return float(f0.mean()), float(f0.std())

    def _speech_rate(self, frame_db: np.ndarray, speech: np.ndarray, hop: int, sr: int) -> Optional[float]:
        """Syllable nuclei ~ peaks of the smoothed energy envelope, >= min gap apart."""
        if not speech.any():
            return None
        k = max(1, int(50 / self.hop_ms))                       # ~50 ms moving average
        env = np.convolve(frame_db, np.ones(k) / k, mode="same")
        env = np.where(speech, env, -120.0)
        min_gap = max(1, int(self.min_syllable_gap_ms / self.hop_ms))
        peaks = np.flatnonzero((env[1:-1] > env[:-2]) & (env[1:-1] >= env[2:])) + 1
        peaks = [p for p in peaks if env[p] > env.max() - 20.0]
        kept: list[int] = []
        for p in peaks:                                           # greedy non-max suppression
            if kept and p - kept[-1] < min_gap:
                if env[p] > env[kept[-1]]:
                    kept[-1] = p
            else:
                kept.append(p)
        speech_s = float(speech.sum()) * hop / sr
        return len(kept) / speech_s if speech_s > 0 else None


def speech_mask(speech: np.ndarray, hop: int, win: int, n: int) -> np.ndarray:
    """Expand frame-level speech flags back to a sample mask."""
    starts = np.flatnonzero(speech) * hop
    diff = np.zeros(n + 1, dtype=np.int32)
    np.add.at(diff, np.minimum(starts, n), 1)
    np.add.at(diff, np.minimum(starts + win, n), -1)
    return np.cumsum(diff[:n]) > 0
