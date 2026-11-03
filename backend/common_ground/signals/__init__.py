"""Perception tier: fast, local signal extractors that feed the interpretation layer."""
from common_ground.signals.acoustic import AcousticExtractor, SpeakerBaseline
from common_ground.signals.linguistic import LinguisticDetector
from common_ground.signals.speech_emotion import SpeechEmotionClassifier
from common_ground.signals.text_emotion import TextEmotionClassifier

__all__ = [
    "AcousticExtractor",
    "LinguisticDetector",
    "SpeakerBaseline",
    "SpeechEmotionClassifier",
    "TextEmotionClassifier",
]
