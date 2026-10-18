"""Translation to the report language (English).

`whisper`  - when a note's detected language is not the target, ask the ASR backend
             for its English rendering (Whisper's `task="translate"`; the sidecar
             backend returns the `text_en` the sample ships). Notes already in the
             target language pass through untouched.
`none`     - passthrough: the source text is used as-is. A note in another language
             is flagged `partial` so the report does not pass it off as translated.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from field_report.backends import BackendError, BackendUnavailable

TARGET = "en"


@dataclass
class Translation:
    en_text: str
    method: str          # "source" | "whisper" | "passthrough"
    translated: bool     # False when the text is still in another language


class WhisperTranslate:
    name = "whisper"

    def __init__(self, asr) -> None:
        self.asr = asr

    def translate(self, text: str, language: str | None, audio: Path) -> Translation:
        if not text.strip() or (language or TARGET) == TARGET:
            return Translation(text, "source", True)
        en = self.asr.translate_to_english(audio)
        if not en:
            raise BackendError("TRANSLATE_EMPTY",
                               f"no English rendering available for a {language!r} note ({audio.name})")
        return Translation(en, "whisper", True)


class NoTranslate:
    name = "none"

    def translate(self, text: str, language: str | None, audio: Path) -> Translation:
        return Translation(text, "passthrough", (language or TARGET) == TARGET)


def get_translator(name: str, asr):
    if name == "whisper":
        return WhisperTranslate(asr)
    if name == "none":
        return NoTranslate()
    raise BackendUnavailable(f"unknown translate backend {name!r}; choose whisper or none")
