"""Text emotion via the int8 ONNX export of SamLowe/roberta-base-go_emotions (28 labels, multi-label).

Runtime deps: onnxruntime + tokenizers only (no torch). Weights (~125 MB) are fetched once with
huggingface_hub into COMMON_GROUND_MODEL_DIR (default ~/.cache/common_ground/models).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import numpy as np

from common_ground.schema import TextEmotion

REPO_ID = "SamLowe/roberta-base-go_emotions-onnx"
MODEL_NAME = "roberta-base-go_emotions-onnx-int8"
_FILES = ("onnx/model_quantized.onnx", "onnx/tokenizer.json", "onnx/config.json")


def default_model_dir() -> Path:
    return Path(os.environ.get("COMMON_GROUND_MODEL_DIR", "~/.cache/common_ground/models")).expanduser()


class TextEmotionClassifier:
    """Lazy-loading, thread-limited ONNX classifier. `classify` returns the top-k sigmoid scores."""

    def __init__(
        self,
        model_dir: Path | None = None,
        top_k: int = 3,
        max_tokens: int = 128,
        threads: int | None = None,
    ) -> None:
        self.model_dir = Path(model_dir) if model_dir else default_model_dir()
        self.top_k = top_k
        self.max_tokens = max_tokens
        self.threads = threads or min(4, os.cpu_count() or 1)
        self._session = None
        self._tokenizer = None
        self._labels: list[str] = []
        self.last_latency_ms: float | None = None

    # ------------------------------------------------------------------ loading
    def _ensure_loaded(self) -> None:
        if self._session is not None:
            return
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer

        local = self.model_dir / REPO_ID.replace("/", "--")
        paths = {f: local / f for f in _FILES}
        if not all(p.is_file() for p in paths.values()):   # first run only: ~128 MB
            paths = {f: Path(hf_hub_download(REPO_ID, f, local_dir=local)) for f in _FILES}

        cfg = json.loads(paths["onnx/config.json"].read_text())
        self._labels = [cfg["id2label"][str(i)] for i in range(len(cfg["id2label"]))]

        tok = Tokenizer.from_file(str(paths["onnx/tokenizer.json"]))
        tok.enable_truncation(self.max_tokens)
        tok.no_padding()
        self._tokenizer = tok

        so = ort.SessionOptions()
        so.intra_op_num_threads = self.threads
        so.inter_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(
            str(paths["onnx/model_quantized.onnx"]), so, providers=["CPUExecutionProvider"]
        )
        self._input_names = {i.name for i in self._session.get_inputs()}

    def warmup(self) -> float:
        """Load weights and run one dummy inference. Returns load+warmup wall time in ms."""
        t0 = time.perf_counter()
        self._ensure_loaded()
        self.classify("warming up the model.")
        return (time.perf_counter() - t0) * 1000.0

    @property
    def labels(self) -> list[str]:
        self._ensure_loaded()
        return list(self._labels)

    # ---------------------------------------------------------------- inference
    def scores(self, text: str) -> np.ndarray:
        """Full 28-way sigmoid probability vector."""
        self._ensure_loaded()
        enc = self._tokenizer.encode(text)
        feeds = {
            "input_ids": np.asarray([enc.ids], dtype=np.int64),
            "attention_mask": np.asarray([enc.attention_mask], dtype=np.int64),
        }
        feeds = {k: v for k, v in feeds.items() if k in self._input_names}
        logits = self._session.run(None, feeds)[0][0].astype(np.float32)
        return 1.0 / (1.0 + np.exp(-logits))

    def classify(self, text: str) -> TextEmotion:
        t0 = time.perf_counter()
        text = text.strip()
        if not text:
            self.last_latency_ms = 0.0
            return TextEmotion(model=MODEL_NAME, top=[])
        probs = self.scores(text)
        idx = np.argsort(-probs)[: self.top_k]
        top = [(self._labels[i], round(float(probs[i]), 4)) for i in idx]
        self.last_latency_ms = (time.perf_counter() - t0) * 1000.0
        return TextEmotion(model=MODEL_NAME, top=top)
