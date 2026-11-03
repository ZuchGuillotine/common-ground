"""Optional heavy path: speech emotion with superb/wav2vec2-base-superb-er (labels neu/hap/ang/sad).

Requires the `audio` extra (torch + transformers, ~380 MB of weights on first use). The import is
guarded so the rest of the signals package works without torch; instantiating this class without
it raises a clear error. The output is evidence, not a verdict: never let it override text signals.
"""
from __future__ import annotations

import os
from typing import Optional

import numpy as np

from common_ground.schema import AcousticEmotion
from common_ground.signals.acoustic import PcmInput, to_float

REPO_ID = "superb/wav2vec2-base-superb-er"
_TARGET_SR = 16000


class SpeechEmotionClassifier:
    def __init__(self, repo_id: str = REPO_ID, threads: Optional[int] = None) -> None:
        self.repo_id = repo_id
        self.threads = threads or min(4, os.cpu_count() or 1)
        self._model = None
        self._extractor = None
        self._torch = None

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        try:
            import torch
            from transformers import AutoFeatureExtractor, AutoModelForAudioClassification
        except ImportError as e:  # pragma: no cover - depends on the environment
            raise ImportError(
                "SpeechEmotionClassifier needs torch + transformers. Install with "
                "`pip install -e 'backend[audio]'` or `pip install torch --index-url "
                "https://download.pytorch.org/whl/cpu && pip install transformers`."
            ) from e
        torch.set_num_threads(self.threads)
        self._torch = torch
        self._extractor = AutoFeatureExtractor.from_pretrained(self.repo_id)
        self._model = AutoModelForAudioClassification.from_pretrained(self.repo_id).eval()

    def warmup(self) -> None:
        self._ensure_loaded()
        self.classify(np.zeros(_TARGET_SR, dtype=np.int16))

    def classify(self, pcm16: PcmInput, sr: int = _TARGET_SR) -> AcousticEmotion:
        self._ensure_loaded()
        x = to_float(pcm16)
        if sr != _TARGET_SR:
            x = _resample_linear(x, sr, _TARGET_SR)
        inputs = self._extractor(x, sampling_rate=_TARGET_SR, return_tensors="pt")
        with self._torch.inference_mode():
            logits = self._model(**inputs).logits[0]
            probs = self._torch.softmax(logits, dim=-1).tolist()
        labels = self._model.config.id2label
        scores = {labels[i]: round(float(p), 4) for i, p in enumerate(probs)}
        label = max(scores, key=scores.get)
        return AcousticEmotion(model=self.repo_id, label=label, scores=scores)


def _resample_linear(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    n_out = int(round(len(x) * sr_out / sr_in))
    t_in = np.linspace(0.0, 1.0, num=len(x), endpoint=False)
    t_out = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(t_out, t_in, x).astype(np.float32)
